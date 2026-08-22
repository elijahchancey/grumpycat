"""GitHub App webhook → resume the parked Step Functions execution for the affected PR.

Events we act on (everything else is 204):

  status                       commit status from external CI (Buildkite posts these)
  check_run  (completed)       GitHub Actions / Checks API
  pull_request_review          submitted: changes_requested / commented-with-body
  pull_request_review_comment  created (inline thread)
  issue_comment                created, on a PR
  pull_request                 closed → merged / closed

The PR is located by number (reviews, comments, check runs) or by branch (`status` events
only carry branches). Actors must be in `reviewer_allowlist` unless they are the PR author
or a repo collaborator is not something we can check here — the allow-list is the rule —
and the bot's own comments are always ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any

from grumpycat.core.models import AwaitedEvent, CIFailure, IssueState, IssueStatus
from grumpycat.handlers import http
from grumpycat.handlers.runtime import Runtime, logger, runtime

ACTIVE = {IssueStatus.PR_OPEN, IssueStatus.SHEPHERDING, IssueStatus.FIXING}
FAILED = {"failure", "error", "cancelled", "timed_out", "action_required"}


def verify(headers: dict[str, str], body: bytes, secret: str) -> bool:
    given = headers.get("x-hub-signature-256", "")
    if not given.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, given)


def _bot_login() -> str:
    return os.environ.get("GRUMPYCAT_BOT_LOGIN", "grumpycat[bot]")


def _allowed(rt: Runtime, login: str | None) -> bool:
    if not login or login == _bot_login():
        return False
    allow = rt.config.reviewer_allowlist
    return not allow or login in allow


def resume(rt: Runtime, state: IssueState, event: AwaitedEvent) -> dict[str, Any]:
    """Hand the event to the execution if it's parked, else queue it for the park step."""
    fp = state.fingerprint
    token = rt.store.wait_token(fp)
    if token:
        rt.store.set_wait_token(fp, None)
        rt.sfn.send_task_success(taskToken=token, output=event.model_dump_json())
        logger.info("resumed", fingerprint=fp, kind=event.kind)
        return {"resumed": fp, "kind": event.kind}
    rt.store.push_pending(fp, event)
    logger.info("queued (execution busy)", fingerprint=fp, kind=event.kind)
    return {"queued": fp, "kind": event.kind}


def _ci_event(
    rt: Runtime, state: IssueState, *, ok: bool, sha: str, context: str, url: str | None
) -> AwaitedEvent:
    now = datetime.now(tz=UTC)
    if ok:
        return AwaitedEvent(kind="ci_success", sha=sha, received_at=now)
    repo = state.target.full_name if state.target else ""
    if rt.registry.ci is not None:
        try:
            failure = rt.registry.ci.fetch_failure(repo, sha, context, url)
        except Exception:
            logger.exception("ci plugin failed to fetch logs", context=context)
            failure = CIFailure(excerpt=f"{context} failed (log fetch failed)", build_url=url)
    else:
        failure = CIFailure(
            excerpt=f"{context} failed; no `ci` plugin configured to fetch logs", build_url=url
        )
    return AwaitedEvent(kind="ci_failure", sha=sha, ci_failure=failure, received_at=now)


def _state_for_pr(rt: Runtime, repo: str, number: int) -> IssueState | None:
    state = rt.store.get_by_pr(repo, number)
    return state if state and state.status in ACTIVE else None


def handle(rt: Runtime, kind: str, p: dict[str, Any]) -> dict[str, Any]:
    repo = str((p.get("repository") or {}).get("full_name") or "")
    now = datetime.now(tz=UTC)

    if kind == "status":
        if p.get("state") == "pending":
            return {"ignored": "pending"}
        for br in p.get("branches") or []:
            name = str(br.get("name") or "")
            if not name.startswith("grumpycat/"):
                continue
            state = rt.store.get_by_branch(repo, name)
            if state is None or state.status not in ACTIVE:
                continue
            ev = _ci_event(
                rt,
                state,
                ok=p.get("state") == "success",
                sha=str(p.get("sha")),
                context=str(p.get("context") or ""),
                url=p.get("target_url"),
            )
            return resume(rt, state, ev)
        return {"ignored": "no grumpycat branch"}

    if kind == "check_run":
        run = p.get("check_run") or {}
        if p.get("action") != "completed":
            return {"ignored": p.get("action")}
        for pr in run.get("pull_requests") or []:
            state = _state_for_pr(rt, repo, int(pr["number"]))
            if state is None:
                continue
            conclusion = str(run.get("conclusion") or "")
            if conclusion in FAILED:
                ev = _ci_event(
                    rt,
                    state,
                    ok=False,
                    sha=str(run.get("head_sha")),
                    context=str(run.get("name") or ""),
                    url=run.get("html_url"),
                )
            elif conclusion == "success":
                ev = _ci_event(
                    rt,
                    state,
                    ok=True,
                    sha=str(run.get("head_sha")),
                    context=str(run.get("name") or ""),
                    url=run.get("html_url"),
                )
            else:
                return {"ignored": conclusion}
            return resume(rt, state, ev)
        return {"ignored": "no tracked PR"}

    if kind == "pull_request_review":
        review = p.get("review") or {}
        if p.get("action") != "submitted":
            return {"ignored": p.get("action")}
        login = (review.get("user") or {}).get("login")
        body = str(review.get("body") or "").strip()
        rstate = str(review.get("state") or "").lower()
        if rstate == "approved" or (rstate == "commented" and not body):
            return {"ignored": rstate}
        if not _allowed(rt, login):
            return {"ignored": f"actor {login}"}
        state = _state_for_pr(rt, repo, int(p["pull_request"]["number"]))
        if state is None:
            return {"ignored": "no tracked PR"}
        ev = AwaitedEvent(
            kind="review", actor=login, findings=[body] if body else [], received_at=now
        )
        return resume(rt, state, ev)

    if kind in {"pull_request_review_comment", "issue_comment"}:
        if p.get("action") != "created":
            return {"ignored": p.get("action")}
        comment = p.get("comment") or {}
        login = (comment.get("user") or {}).get("login")
        if not _allowed(rt, login):
            return {"ignored": f"actor {login}"}
        if kind == "issue_comment":
            issue = p.get("issue") or {}
            if not issue.get("pull_request"):
                return {"ignored": "not a PR"}
            number = int(issue["number"])
        else:
            number = int(p["pull_request"]["number"])
        state = _state_for_pr(rt, repo, number)
        if state is None:
            return {"ignored": "no tracked PR"}
        text = str(comment.get("body") or "").strip()
        if kind == "pull_request_review_comment" and comment.get("path"):
            line = comment.get("line") or comment.get("original_line") or "?"
            text = f"{comment['path']}:{line}\n{text}"
        ids = (
            [int(comment["id"])]
            if kind == "pull_request_review_comment" and comment.get("id")
            else []
        )
        ev = AwaitedEvent(
            kind="comment", actor=login, findings=[text], comment_ids=ids, received_at=now
        )
        return resume(rt, state, ev)

    if kind == "pull_request":
        if p.get("action") != "closed":
            return {"ignored": p.get("action")}
        pr = p.get("pull_request") or {}
        state = _state_for_pr(rt, repo, int(pr["number"]))
        if state is None:
            return {"ignored": "no tracked PR"}
        merged = bool(pr.get("merged"))
        ev = AwaitedEvent(
            kind="merged" if merged else "closed",
            actor=(pr.get("merged_by") or {}).get("login"),
            received_at=now,
        )
        return resume(rt, state, ev)

    return {"ignored": kind}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    rt = runtime()
    hdrs = http.headers(event)
    raw = http.body(event)
    secret = rt.registry.secrets.get("GITHUB_WEBHOOK_SECRET")
    if not secret or not verify(hdrs, raw, secret):
        logger.warning("github webhook signature rejected")
        return http.respond(401, {"error": "invalid signature"})
    kind = hdrs.get("x-github-event", "")
    if kind == "ping":
        return http.respond(200, {"pong": True})
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return http.respond(400, {"error": "body is not JSON"})
    result = handle(rt, kind, payload)
    return http.respond(200, result)
