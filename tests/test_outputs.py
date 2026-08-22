from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from pydantic import HttpUrl

from grumpycat.ci.buildkite import BuildkiteCI, BuildkiteConfig
from grumpycat.core import github_auth
from grumpycat.core.models import (
    Brief,
    Evidence,
    FixOutcome,
    IssueStatus,
    Severity,
    Triage,
)
from grumpycat.handlers import slack_interactions
from grumpycat.handlers.runtime import Runtime
from grumpycat.outputs.github import GitHubConfig, GitHubOutput, pr_body, pr_title
from grumpycat.outputs.slack import APPROVE, DISMISS, SlackConfig, SlackOutput
from tests.conftest import http_event, make_event, make_state

GH_SECRETS = {
    "GITHUB_APP_ID": "1",
    "GITHUB_APP_INSTALLATION_ID": "2",
    "GITHUB_APP_PRIVATE_KEY": "k",
}


class Recorder:
    """httpx mock that records calls and answers by (method, path)."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.calls.append((req.method, req.url.path, body))
        key = f"{req.method} {req.url.path}"
        for k, v in self.routes.items():
            if key.endswith(k):
                return httpx.Response(200, json=v(body) if callable(v) else v)
        return httpx.Response(200, json={})

    def paths(self) -> list[str]:
        return [f"{m} {p}" for m, p, _ in self.calls]


@pytest.fixture(autouse=True)
def app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_auth, "installation_token", lambda *a, **k: "ghs_x")


def brief_for(state: Any) -> Brief:
    return Brief(
        event=state.event,
        evidence=Evidence(routing_hints=["culprit: app/x.rb"]),
        triage=Triage(severity=Severity.HIGH, confidence=0.7, page=False, rationale="r"),
        target=state.target,
    )


# -- github -------------------------------------------------------------------------------


def test_github_opens_draft_pr_on_pr_open_transition() -> None:
    rec = Recorder(
        {
            "POST /repos/acme/api/pulls": {
                "number": 42,
                "html_url": "https://github.com/acme/api/pull/42",
            }
        }
    )
    out = GitHubOutput(GitHubConfig(extra_labels=["bot"]), GH_SECRETS)
    out.transport = httpx.MockTransport(rec)
    state = make_state(
        status=IssueStatus.PR_OPEN,
        branch="grumpycat/fake-abc",
        triage=brief_for(make_state()).triage,
    )
    new = out.on_transition(state, make_state(status=IssueStatus.FIXING), brief_for(state))
    assert new.pr_number == 42 and str(new.pr_url) == "https://github.com/acme/api/pull/42"
    assert rec.paths() == ["POST /repos/acme/api/pulls", "POST /repos/acme/api/issues/42/labels"]
    pr = rec.calls[0][2]
    assert pr["draft"] is True and pr["head"] == "grumpycat/fake-abc" and pr["base"] == "master"
    assert pr["title"] == "Fix: NoMethodError: boom"
    assert "## Review notes" in pr["body"] and "culprit: app/x.rb" in pr["body"]
    assert rec.calls[1][2] == {"labels": ["grumpycat", "bot"]}
    # already has a PR -> no-op
    rec.calls.clear()
    out.on_transition(new, state, None)
    assert rec.calls == []


def test_github_after_push_comments_replies_and_resolves_threads() -> None:
    threads = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "T1",
                                "isResolved": False,
                                "comments": {"nodes": [{"databaseId": 501}]},
                            },
                            {
                                "id": "T2",
                                "isResolved": True,
                                "comments": {"nodes": [{"databaseId": 502}]},
                            },
                        ]
                    }
                }
            }
        }
    }
    graphql_calls: list[dict[str, Any]] = []

    def graphql(body: dict[str, Any]) -> dict[str, Any]:
        graphql_calls.append(body)
        return (
            threads
            if "reviewThreads" in body["query"]
            else {"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}}
        )

    rec = Recorder({"POST /graphql": graphql})
    out = GitHubOutput(GitHubConfig(), GH_SECRETS)
    out.transport = httpx.MockTransport(rec)
    outcome = FixOutcome(
        status="pushed",
        pushed_sha="abcdef1234567890",
        summary="nil guard",
        addressed_comment_ids=[501, 502],
        report="declined one nit",
    )
    prev = make_state(status=IssueStatus.PR_OPEN, pr_number=42)
    state = make_state(status=IssueStatus.GROOMING, pr_number=42, attempts=2, last_outcome=outcome)
    out.on_transition(state, prev, None)
    paths = rec.paths()
    assert paths[0] == "POST /repos/acme/api/issues/42/comments"
    comment = rec.calls[0][2]["body"]
    assert (
        "Pushed `abcdef1234` (attempt 2)" in comment
        and "nil guard" in comment
        and "Engine report" in comment
    )
    assert paths.count("POST /repos/acme/api/pulls/42/comments/501/replies") == 1
    assert paths.count("POST /repos/acme/api/pulls/42/comments/502/replies") == 1
    resolves = [g for g in graphql_calls if "resolveReviewThread" in g["query"]]
    assert [g["variables"]["id"] for g in resolves] == ["T1"]  # T2 already resolved
    # same outcome again (no new push) -> nothing
    rec.calls.clear()
    out.on_transition(state, state, None)
    assert rec.calls == []


def test_github_ready_and_needs_human() -> None:
    rec = Recorder({"GET /repos/acme/api/pulls/42": {"node_id": "PR_node"}})
    out = GitHubOutput(
        GitHubConfig(request_reviewers_on_ready=["alice", "acme/platform"]), GH_SECRETS
    )
    out.transport = httpx.MockTransport(rec)
    st = make_state(
        status=IssueStatus.READY,
        pr_number=42,
        pr_url=HttpUrl("https://github.com/acme/api/pull/42"),
    )
    out.on_transition(st, make_state(status=IssueStatus.GROOMING, pr_number=42), None)
    paths = rec.paths()
    assert "POST /graphql" in paths and "POST /repos/acme/api/pulls/42/requested_reviewers" in paths
    rr = next(b for m, p, b in rec.calls if p.endswith("requested_reviewers"))
    assert rr == {"reviewers": ["alice"], "team_reviewers": ["platform"]}
    assert any(
        "ready for review" in (b or {}).get("body", "")
        for _, p, b in rec.calls
        if p.endswith("/comments")
    )

    rec.calls.clear()
    nh = make_state(
        status=IssueStatus.NEEDS_HUMAN, pr_number=42, rationale="attempt budget (3) exhausted"
    )
    out.on_transition(nh, st, None)
    assert rec.calls[0][1].endswith("/issues/42/labels") and rec.calls[0][2] == {
        "labels": ["needs-human"]
    }
    assert "attempt budget" in rec.calls[1][2]["body"]
    # no PR yet -> nothing to label
    rec.calls.clear()
    out.on_transition(make_state(status=IssueStatus.NEEDS_HUMAN), None, None)
    assert rec.calls == []


def test_pr_title_and_body_for_regression() -> None:
    from grumpycat.core.models import Transition

    st = make_state(event=make_event(transition=Transition.REGRESSION, title="Boom"))
    assert pr_title(st) == "Fix regression: Boom"
    b = brief_for(st).model_copy(
        update={"previous_pr_url": HttpUrl("https://github.com/acme/api/pull/1")}
    )
    assert "earlier fix: https://github.com/acme/api/pull/1" in pr_body(st, b)


# -- slack --------------------------------------------------------------------------------


def slack_output(rec: Recorder, **cfg: Any) -> SlackOutput:
    out = SlackOutput(
        SlackConfig(channel="C123", **cfg),
        {"SLACK_BOT_TOKEN": "xoxb", "SLACK_SIGNING_SECRET": "ss"},
    )
    out.transport = httpx.MockTransport(rec)
    return out


def test_slack_awaiting_approval_posts_buttons_and_pages_once() -> None:
    n = {"i": 0}

    def post(body: dict[str, Any]) -> dict[str, Any]:
        n["i"] += 1
        return {"ok": True, "ts": f"170000000{n['i']}.000"}

    rec = Recorder({"/chat.postMessage": post})
    out = slack_output(rec, oncall_channel="C-ONCALL")
    tri = Triage(severity=Severity.CRITICAL, confidence=0.8, page=True, rationale="57 users")
    st = make_state(status=IssueStatus.AWAITING_APPROVAL, triage=tri)
    new = out.on_transition(st, None, None)
    assert new.paged is True and new.slack_thread_ts == "1700000002.000"
    page, main = rec.calls[0][2], rec.calls[1][2]
    assert page["channel"] == "C-ONCALL" and ":rotating_light:" in page["text"]
    assert main["channel"] == "C123"
    ids = [e["action_id"] for e in main["blocks"][1]["elements"]]
    assert ids == [APPROVE, DISMISS] and main["blocks"][1]["elements"][0]["value"] == "fake:1"
    # later transitions reply in-thread and never page again
    rec.calls.clear()
    fixing = new.model_copy(update={"status": IssueStatus.FIXING})
    out.on_transition(fixing, new, None)
    assert len(rec.calls) == 1 and rec.calls[0][2]["thread_ts"] == "1700000002.000"
    assert "starting a fix run" in rec.calls[0][2]["text"]


def test_slack_thread_replies_per_transition_and_rca_only() -> None:
    rec = Recorder({"/chat.postMessage": {"ok": True, "ts": "1.0"}})
    out = slack_output(rec)
    base = make_state(status=IssueStatus.FIXING, slack_thread_ts="1.0")
    steps = [
        (
            IssueStatus.PR_OPEN,
            {"pr_url": HttpUrl("https://github.com/acme/api/pull/7")},
            "draft PR opened",
        ),
        (
            IssueStatus.GROOMING,
            {"attempts": 2, "last_outcome": FixOutcome(status="pushed", pushed_sha="abcdef123456")},
            "pushed follow-up `abcdef1234` (attempt 2)",
        ),
        (
            IssueStatus.READY,
            {"pr_url": HttpUrl("https://github.com/acme/api/pull/7")},
            "ready for review",
        ),
        (IssueStatus.NEEDS_HUMAN, {"rationale": "budget"}, "needs a human — budget"),
        (IssueStatus.MERGED, {}, "merged"),
    ]
    prev = base
    for status, extra, needle in steps:
        st = prev.model_copy(update={"status": status, **extra})
        out.on_transition(st, prev, None)
        assert needle in rec.calls[-1][2]["text"], needle
        assert rec.calls[-1][2]["thread_ts"] == "1.0"
        prev = st
    # same status again = "seen again"
    out.on_transition(
        prev.model_copy(update={"status": IssueStatus.READY}),
        prev.model_copy(update={"status": IssueStatus.READY}),
        None,
    )
    assert "seen again" in rec.calls[-1][2]["text"]
    # rca-only without a thread starts one
    rec.calls.clear()
    st = make_state(status=IssueStatus.RCA_ONLY, rationale="confidence 0.2 < 0.6")
    new = out.on_transition(st, None, None)
    assert new.slack_thread_ts == "1.0" and "No PR: confidence" in rec.calls[0][2]["text"]


def test_slack_api_error_raises() -> None:
    rec = Recorder({"/chat.postMessage": {"ok": False, "error": "channel_not_found"}})
    with pytest.raises(RuntimeError, match="channel_not_found"):
        slack_output(rec).on_transition(
            make_state(status=IssueStatus.AWAITING_APPROVAL), None, None
        )


# -- slack interactions ----------------------------------------------------------------------


class _Ctx:
    function_name = "t"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:us-east-1:1:function:t"
    aws_request_id = "r"


def slack_event(
    payload: dict[str, Any], secret: str = "ss", ts: int | None = None
) -> dict[str, Any]:
    body = urlencode({"payload": json.dumps(payload)})
    t = str(ts or int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{t}:{body}".encode(), hashlib.sha256).hexdigest()
    return http_event(
        "/slack/interactions", body, {"x-slack-signature": sig, "x-slack-request-timestamp": t}
    )


def test_slack_interactions_verify_and_approve(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt.registry.secrets = {**rt.registry.secrets, "SLACK_SIGNING_SECRET": "ss"}  # type: ignore[misc]
    monkeypatch.setattr(slack_interactions, "_respond", lambda url, text: None)
    st = make_state(status=IssueStatus.AWAITING_APPROVAL, brief=brief_for(make_state()))
    rt.store.put(st)
    payload = {
        "actions": [{"action_id": APPROVE, "value": "fake:1"}],
        "user": {"username": "alice"},
        "response_url": "https://hooks.example.test/x",
    }
    assert (
        slack_interactions.handler(slack_event(payload, secret="wrong"), _Ctx())["statusCode"]
        == 401
    )
    assert (
        slack_interactions.handler(slack_event(payload, ts=int(time.time()) - 1000), _Ctx())[
            "statusCode"
        ]
        == 401
    )
    r = slack_interactions.handler(slack_event(payload), _Ctx())
    assert r["statusCode"] == 200 and json.loads(r["body"])["started"] == "fake:1"
    assert rt.store.get("fake:1").status is IssueStatus.FIXING  # type: ignore[union-attr]
    rt.sfn.start_execution.assert_called_once()
    # second click: already fixing
    r = slack_interactions.handler(slack_event(payload), _Ctx())
    assert json.loads(r["body"]) == {"ignored": "status fixing"}


def test_slack_interactions_dismiss(rt: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    rt.registry.secrets = {**rt.registry.secrets, "SLACK_SIGNING_SECRET": "ss"}  # type: ignore[misc]
    monkeypatch.setattr(slack_interactions, "_respond", lambda url, text: None)
    rt.store.put(make_state(status=IssueStatus.AWAITING_APPROVAL, brief=brief_for(make_state())))
    payload = {"actions": [{"action_id": DISMISS, "value": "fake:1"}], "user": {"id": "U1"}}
    r = slack_interactions.handler(slack_event(payload), _Ctx())
    assert json.loads(r["body"]) == {"dismissed": "fake:1"}
    st = rt.store.get("fake:1")
    assert (
        st is not None
        and st.status is IssueStatus.CLOSED
        and "dismissed by U1" in (st.rationale or "")
    )
    assert json.loads(
        slack_interactions.handler(
            slack_event({"actions": [{"action_id": APPROVE, "value": "nope"}]}), _Ctx()
        )["body"]
    ) == {"ignored": "unknown fingerprint"}


# -- buildkite ci -----------------------------------------------------------------------------


def test_buildkite_fetches_failed_job_log_tail() -> None:
    build = {
        "state": "failed",
        "web_url": "https://buildkite.com/acme/api/builds/9",
        "jobs": [
            {"id": "j1", "type": "script", "state": "passed", "name": "lint"},
            {
                "id": "j2",
                "type": "script",
                "state": "failed",
                "name": "rspec",
                "web_url": "https://buildkite.com/acme/api/builds/9#j2",
            },
            {"id": "j3", "type": "script", "state": "failed", "name": "types"},
            {"id": "j4", "type": "waiter", "state": "failed"},
        ],
    }
    log = {"content": "\x1b[31mFailures:\x1b[0m\n  1) deliver\n     expected nil\n" + ("x" * 50)}
    rec = Recorder(
        {
            "GET /v2/organizations/acme/pipelines/api/builds/9": build,
            "GET /v2/organizations/acme/pipelines/api/builds/9/jobs/j2/log": log,
        }
    )
    ci = BuildkiteCI(BuildkiteConfig(tail_bytes=1000), {"BUILDKITE_API_TOKEN": "bk"})
    ci.transport = httpx.MockTransport(rec)
    f = ci.fetch_failure(
        "acme/api", "abc", "buildkite/api", "https://buildkite.com/acme/api/builds/9"
    )
    assert f.job_name == "rspec (+1 more failed jobs)"
    assert f.excerpt.startswith("Failures:\n  1) deliver") and "\x1b" not in f.excerpt
    assert str(f.build_url).endswith("#j2") and f.truncated is False
    assert rec.paths() == [
        "GET /v2/organizations/acme/pipelines/api/builds/9",
        "GET /v2/organizations/acme/pipelines/api/builds/9/jobs/j2/log",
    ]

    small = BuildkiteCI(BuildkiteConfig(tail_bytes=1000), {"BUILDKITE_API_TOKEN": "bk"})
    small.transport = httpx.MockTransport(
        Recorder({"builds/9": build, "/log": {"content": "y" * 5000}})
    )
    g = small.fetch_failure("acme/api", "abc", "ctx", "https://buildkite.com/acme/api/builds/9")
    assert g.truncated is True and len(g.excerpt) == 1000


def test_buildkite_handles_missing_url_and_no_failed_jobs() -> None:
    ci = BuildkiteCI(BuildkiteConfig(), {"BUILDKITE_API_TOKEN": "bk"})
    f = ci.fetch_failure("acme/api", "abc", "ctx", None)
    assert "no Buildkite build URL" in f.excerpt and f.build_url is None
    ci.transport = httpx.MockTransport(
        Recorder(
            {
                "builds/9": {
                    "state": "canceled",
                    "jobs": [],
                    "web_url": "https://buildkite.com/acme/api/builds/9",
                }
            }
        )
    )
    g = ci.fetch_failure("acme/api", "abc", "ctx", "https://buildkite.com/acme/api/builds/9")
    assert "no failed script jobs" in g.excerpt
