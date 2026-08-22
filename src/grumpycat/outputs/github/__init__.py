"""GitHub output: the PR lifecycle, driven by issue-state transitions.

  PR_OPEN (no pr_number yet)  open a draft PR from the pushed branch, label it
  GROOMING (after a push) comment what was pushed; reply to and resolve the review
                              threads that push addressed; post the engine's report for
                              anything it declined
  READY                       mark ready for review, comment
  NEEDS_HUMAN                 label `needs-human`, comment the reason
  CLOSED / MERGED             nothing (GitHub already knows)

Never merges. Authenticates as the GitHub App (installation token per call).
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from grumpycat.core import github_auth
from grumpycat.core.models import Brief, IssueState, IssueStatus, Transition
from grumpycat.plugins.spec import OutputPlugin, PluginKind, PluginSpec

API_VERSION = "2022-11-28"


class GitHubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: HttpUrl = Field(default=HttpUrl("https://api.github.com"))
    draft: bool = True
    needs_human_label: str = "needs-human"
    extra_labels: list[str] = Field(default_factory=list)
    request_reviewers_on_ready: list[str] = Field(
        default_factory=list, description="Logins or team slugs (org/team) to request"
    )


class GitHubOutput(OutputPlugin):
    spec = PluginSpec(
        name="github",
        kind=PluginKind.OUTPUT,
        config_schema=GitHubConfig,
        required_secrets=github_auth.REQUIRED,
        optional_tools=("gh",),
    )
    config: GitHubConfig
    transport: Any = None

    # -- http -----------------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        token = github_auth.installation_token(
            self.secrets, api_url=str(self.config.api_url).rstrip("/"), transport=self.transport
        )
        return httpx.Client(
            base_url=str(self.config.api_url).rstrip("/"),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            timeout=30.0,
            transport=self.transport,
        )

    # -- transitions ----------------------------------------------------------------------

    def on_transition(
        self, state: IssueState, previous: IssueState | None, brief: Brief | None
    ) -> IssueState:
        if state.target is None:
            return state
        prev_status = previous.status if previous else None
        if state.status is IssueStatus.PR_OPEN and state.pr_number is None and state.branch:
            return self._open_pr(state, brief or state.brief)
        if state.status is IssueStatus.GROOMING and state.pr_number and _new_push(state, previous):
            self._after_push(state)
        elif state.status is IssueStatus.READY and prev_status is not IssueStatus.READY:
            self._ready(state)
        elif state.status is IssueStatus.NEEDS_HUMAN and prev_status is not IssueStatus.NEEDS_HUMAN:
            self._needs_human(state)
        return state

    def _open_pr(self, state: IssueState, brief: Brief | None) -> IssueState:
        assert state.target is not None
        repo = state.target.full_name
        with self._client() as c:
            r = c.post(
                f"/repos/{repo}/pulls",
                json={
                    "title": pr_title(state),
                    "head": state.branch,
                    "base": state.target.default_branch,
                    "body": pr_body(state, brief),
                    "draft": self.config.draft,
                },
            )
            r.raise_for_status()
            pr = r.json()
            labels = [*state.target.labels, *self.config.extra_labels]
            if labels:
                c.post(
                    f"/repos/{repo}/issues/{pr['number']}/labels", json={"labels": labels}
                ).raise_for_status()
        return state.model_copy(update={"pr_number": int(pr["number"]), "pr_url": pr["html_url"]})

    def _after_push(self, state: IssueState) -> None:
        assert state.target is not None and state.pr_number is not None
        repo = state.target.full_name
        out = state.last_outcome
        sha = out.pushed_sha[:10] if out and out.pushed_sha else "a new commit"
        lines = [f"Pushed `{sha}` (attempt {state.attempts})."]
        if out and out.summary:
            lines += ["", out.summary[:1500]]
        if out and out.report:
            lines += [
                "",
                "<details><summary>Engine report</summary>",
                "",
                out.report[:6000],
                "",
                "</details>",
            ]
        with self._client() as c:
            c.post(
                f"/repos/{repo}/issues/{state.pr_number}/comments", json={"body": "\n".join(lines)}
            ).raise_for_status()
            for cid in out.addressed_comment_ids if out else []:
                self._reply_and_resolve(c, repo, state.pr_number, cid, f"Addressed in `{sha}`.")

    def _reply_and_resolve(
        self, c: httpx.Client, repo: str, pr: int, comment_id: int, text: str
    ) -> None:
        c.post(
            f"/repos/{repo}/pulls/{pr}/comments/{comment_id}/replies", json={"body": text}
        ).raise_for_status()
        thread_id = self._thread_id_for(c, repo, pr, comment_id)
        if thread_id:
            c.post(
                "/graphql",
                json={
                    "query": (
                        "mutation($id: ID!) { resolveReviewThread(input: {threadId: $id})"
                        " { thread { isResolved } } }"
                    ),
                    "variables": {"id": thread_id},
                },
            ).raise_for_status()

    def _thread_id_for(self, c: httpx.Client, repo: str, pr: int, comment_id: int) -> str | None:
        owner, name = repo.split("/", 1)
        q = (
            "query($owner: String!, $name: String!, $pr: Int!) {"
            " repository(owner: $owner, name: $name) {"
            " pullRequest(number: $pr) { reviewThreads(first: 100) { nodes { id isResolved"
            " comments(first: 50) { nodes { databaseId } } } } } } }"
        )
        r = c.post(
            "/graphql", json={"query": q, "variables": {"owner": owner, "name": name, "pr": pr}}
        )
        r.raise_for_status()
        threads = (
            r.json()
            .get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
            .get("nodes", [])
        )
        for t in threads:
            if any(
                cm.get("databaseId") == comment_id for cm in t.get("comments", {}).get("nodes", [])
            ):
                return str(t["id"]) if not t.get("isResolved") else None
        return None

    def _ready(self, state: IssueState) -> None:
        assert state.target is not None and state.pr_number is not None
        repo = state.target.full_name
        with self._client() as c:
            node_id = (
                c.get(f"/repos/{repo}/pulls/{state.pr_number}")
                .raise_for_status()
                .json()
                .get("node_id")
            )
            if node_id and self.config.draft:
                c.post(
                    "/graphql",
                    json={
                        "query": (
                            "mutation($id: ID!) { markPullRequestReadyForReview("
                            "input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
                        ),
                        "variables": {"id": node_id},
                    },
                ).raise_for_status()
            if self.config.request_reviewers_on_ready:
                users = [r for r in self.config.request_reviewers_on_ready if "/" not in r]
                teams = [
                    r.split("/", 1)[1] for r in self.config.request_reviewers_on_ready if "/" in r
                ]
                c.post(
                    f"/repos/{repo}/pulls/{state.pr_number}/requested_reviewers",
                    json={"reviewers": users, "team_reviewers": teams},
                ).raise_for_status()
            c.post(
                f"/repos/{repo}/issues/{state.pr_number}/comments",
                json={
                    "body": (
                        "CI is green and no review thread is open — marked ready for review. "
                        "Merging is yours."
                    )
                },
            ).raise_for_status()

    def _needs_human(self, state: IssueState) -> None:
        if state.target is None or state.pr_number is None:
            return
        repo = state.target.full_name
        with self._client() as c:
            c.post(
                f"/repos/{repo}/issues/{state.pr_number}/labels",
                json={"labels": [self.config.needs_human_label]},
            ).raise_for_status()
            c.post(
                f"/repos/{repo}/issues/{state.pr_number}/comments",
                json={
                    "body": (
                        f"grumpycat is handing this off: {state.rationale or 'stuck'}. "
                        "It will not push again on this PR."
                    )
                },
            ).raise_for_status()


def _new_push(state: IssueState, previous: IssueState | None) -> bool:
    if state.last_outcome is None or state.last_outcome.status != "pushed":
        return False
    prev_sha = previous.last_outcome.pushed_sha if previous and previous.last_outcome else None
    return (
        state.last_outcome.pushed_sha != prev_sha
        or previous is None
        or previous.status is not IssueStatus.GROOMING
    )


def pr_title(state: IssueState) -> str:
    kind = "Fix regression" if state.event.transition is Transition.REGRESSION else "Fix"
    return f"{kind}: {state.event.title}"[:120]


def pr_body(state: IssueState, brief: Brief | None) -> str:
    e = state.event
    t = state.triage
    lines = [
        "## What",
        "",
        f"Automated fix for a production error reported by **{e.source}**.",
        "",
        f"- Source: {e.url or e.fingerprint}",
        f"- Service: `{e.service or '?'}` · Env: `{e.env or '?'}` · "
        f"First seen: {e.occurred_at.date().isoformat()}",
    ]
    if t:
        lines.append(
            f"- Triage: severity **{t.severity}**, confidence {t.confidence:.2f} — {t.rationale}"
        )
    if brief:
        ev = brief.evidence
        if ev.exception:
            lines += [
                "",
                "## Error",
                "",
                "```",
                f"{ev.exception.type}: {ev.exception.value}"[:1500],
                "```",
            ]
        if ev.routing_hints:
            lines += ["", "## Where", "", *[f"- {h}" for h in ev.routing_hints]]
        if brief.previous_pr_url:
            lines += [
                "",
                f"Regression of a previously fixed issue; earlier fix: {brief.previous_pr_url}",
            ]
    if state.last_outcome and state.last_outcome.summary:
        lines += ["", "## Change summary (from the engine)", "", state.last_outcome.summary[:3000]]
    lines += [
        "",
        "## Review notes",
        "",
        "- Opened as a draft by grumpycat; CI and review comments are followed automatically.",
        "- Comment on the PR to request changes; the bot replies on the threads it addresses.",
        "- Merging is always a human decision.",
        "",
        f"<sub>fingerprint `{e.fingerprint}`</sub>",
    ]
    return "\n".join(lines)
