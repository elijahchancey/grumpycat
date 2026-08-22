from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import HttpUrl

from grumpycat.ci.github_actions import GitHubActionsCI, GitHubActionsConfig
from grumpycat.core import github_auth
from grumpycat.core.models import IssueStatus
from grumpycat.handlers import digest
from grumpycat.handlers.runtime import Runtime
from grumpycat.plugins import PluginKind, build
from grumpycat.plugins.spec import CIPlugin
from tests.conftest import make_state

GH = {"GITHUB_APP_ID": "1", "GITHUB_APP_INSTALLATION_ID": "2", "GITHUB_APP_PRIVATE_KEY": "k"}


class _Ctx:
    function_name = "t"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:us-east-1:1:function:t"
    aws_request_id = "r"


@pytest.fixture(autouse=True)
def app_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_auth, "installation_token", lambda *a, **k: "ghs_x")


# -- github_actions ci ----------------------------------------------------------------------


def test_gha_from_job_url_and_run_url() -> None:
    log = "2026-08-22T10:00:00.0000000Z \x1b[31mFAIL\x1b[0m tests/test_x.py::test_y\n" + ("z" * 30)

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/actions/jobs/77"):
            return httpx.Response(
                200,
                json={
                    "name": "tests (py3.14)",
                    "html_url": "https://github.com/acme/api/actions/runs/5/job/77",
                },
            )
        if p.endswith("/actions/jobs/77/logs"):
            return httpx.Response(200, text=log)
        if p.endswith("/actions/runs/5/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"id": 76, "conclusion": "success", "name": "lint"},
                        {
                            "id": 77,
                            "conclusion": "failure",
                            "name": "tests (py3.14)",
                            "html_url": "https://github.com/acme/api/actions/runs/5/job/77",
                        },
                        {"id": 78, "conclusion": "failure", "name": "types"},
                    ]
                },
            )
        return httpx.Response(404)

    ci = GitHubActionsCI(GitHubActionsConfig(tail_bytes=1000), GH)
    ci.transport = httpx.MockTransport(handler)
    f = ci.fetch_failure(
        "acme/api", "abc", "tests (py3.14)", "https://github.com/acme/api/actions/runs/5/job/77"
    )
    assert f.job_name == "tests (py3.14)" and f.excerpt.startswith("FAIL tests/test_x.py::test_y")
    assert "\x1b" not in f.excerpt and "2026-08-22T" not in f.excerpt
    g = ci.fetch_failure("acme/api", "abc", "ci", "https://github.com/acme/api/actions/runs/5")
    assert g.job_name == "tests (py3.14) (+1 more failed jobs)" and str(g.build_url).endswith(
        "/job/77"
    )
    assert "no Actions run URL" in ci.fetch_failure("acme/api", "abc", "ci", None).excerpt
    assert isinstance(build(PluginKind.CI, "github_actions", {}, GH, cls=CIPlugin), GitHubActionsCI)


def test_gha_truncates_and_handles_no_failed_jobs() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/jobs"):
            return httpx.Response(200, json={"jobs": [{"id": 1, "conclusion": "success"}]})
        if req.url.path.endswith("/logs"):
            return httpx.Response(200, text="y" * 5000)
        return httpx.Response(
            200, json={"name": "j", "html_url": "https://github.com/acme/api/actions/runs/5/job/9"}
        )

    ci = GitHubActionsCI(GitHubActionsConfig(tail_bytes=1000), GH)
    ci.transport = httpx.MockTransport(handler)
    assert (
        "no failed jobs"
        in ci.fetch_failure(
            "acme/api", "a", "c", "https://github.com/acme/api/actions/runs/5"
        ).excerpt
    )
    f = ci.fetch_failure("acme/api", "a", "c", "https://github.com/acme/api/actions/runs/5/job/9")
    assert f.truncated is True and len(f.excerpt) == 1000


# -- digest ---------------------------------------------------------------------------------


def test_digest_render_groups_by_repo_and_counts() -> None:
    now = datetime.now(tz=UTC)
    states = [
        make_state(
            fingerprint="a",
            status=IssueStatus.MERGED,
            pr_number=1,
            pr_url=HttpUrl("https://github.com/acme/api/pull/1"),
            cost_usd=1.5,
            updated_at=now,
        ),
        make_state(
            fingerprint="b",
            status=IssueStatus.NEEDS_HUMAN,
            rationale="budget",
            cost_usd=2.0,
            updated_at=now,
        ),
        make_state(
            fingerprint="c",
            status=IssueStatus.RCA_ONLY,
            rationale="low confidence",
            target=None,
            updated_at=now,
        ),
    ]
    text = digest.render(states, 24, "acme")
    assert "3 issue(s), 1 merged, 0 ready, 1 need a human, 1 RCA-only, $3.50" in text
    assert "*acme/api* (2)" in text and "*(unmapped)* (1)" in text
    assert "<https://github.com/acme/api/pull/1|PR #1>" in text and "_budget_" in text
    assert text.index("`merged`") < text.index("`needs_human`")
    assert "nothing happened" in digest.render([], 12, "acme")


def test_digest_handler_scans_recent_and_posts_to_slack(
    rt: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(tz=UTC)
    rt.store.put(make_state(fingerprint="new", status=IssueStatus.READY, updated_at=now))
    old = make_state(
        fingerprint="old", status=IssueStatus.MERGED, updated_at=now - timedelta(days=3)
    )
    rt.store.put(old)
    rt.store.table.update_item(
        Key={"pk": "ISSUE#old"},
        UpdateExpression="SET updated_at = :t",
        ExpressionAttributeValues={":t": (now - timedelta(days=3)).isoformat()},
    )
    posted: list[tuple[str, str]] = []

    class FakeSlack:
        config = type("C", (), {"channel": "C1"})()

        def _post(self, channel: str, text: str, **kw: Any) -> str:
            posted.append((channel, text))
            return "1.0"

    rt.registry.outputs["slack"] = FakeSlack()  # type: ignore[assignment]
    out = digest.handler({"hours": 24}, _Ctx())
    assert out["issues"] == 1 and out["posted"] is True
    assert posted[0][0] == "C1" and "1 issue(s)" in posted[0][1] and "`ready`" in posted[0][1]
    del rt.registry.outputs["slack"]
    out = digest.handler({}, _Ctx())
    assert out["posted"] is False and "1 issue(s)" in json.dumps(out["text"])
