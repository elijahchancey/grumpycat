from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import HttpUrl

from grumpycat.core.models import AwaitedEvent, FixOutcome, IssueStatus
from grumpycat.core.secrets import load_secrets
from grumpycat.handlers import github_hook, lifecycle, router, triage
from grumpycat.handlers.runtime import Runtime
from tests.conftest import http_event, make_state


class _Ctx:
    function_name = "grumpycat-test"
    memory_limit_in_mb = 128
    invoked_function_arn = "arn:aws:lambda:us-east-1:123:function:grumpycat-test"
    aws_request_id = "req-1"


CTX: Any = _Ctx()


# -- secrets ------------------------------------------------------------------------------


def test_load_secrets_from_ssm_and_secrets_manager(aws: Any) -> None:
    ssm = aws.client("ssm")
    sm = aws.client("secretsmanager")
    ssm.put_parameter(Name="/grumpycat/a", Value="from-ssm", Type="SecureString")
    arn = sm.create_secret(Name="grumpycat/b", SecretString=json.dumps({"k": "from-sm"}))["ARN"]
    env = {
        "GRUMPYCAT_SECRET_ARNS": json.dumps(
            {
                "A": "arn:aws:ssm:us-east-1:123:parameter/grumpycat/a",
                "B": f"{arn}:k::",
                "C": "arn:aws:ssm:us-east-1:123:parameter/grumpycat/missing",
            }
        ),
        "C": "already-in-env",
    }
    assert load_secrets(env, ssm=ssm, sm=sm) == {
        "A": "from-ssm",
        "B": "from-sm",
        "C": "already-in-env",
    }
    with pytest.raises(ValueError, match="unsupported secret reference"):
        load_secrets({"GRUMPYCAT_SECRET_ARNS": json.dumps({"X": "nope"})}, ssm=ssm, sm=sm)


# -- router -------------------------------------------------------------------------------


def test_router_http_verifies_parses_and_dispatches(rt: Runtime) -> None:
    ok = {"x-fake-sig": "ok"}
    r = router.handler(http_event("/in/fake_in", {"fp": "1"}, ok), CTX)
    assert r["statusCode"] == 202
    assert json.loads(r["body"]) == {"fingerprint": "fake:1", "transition": "new"}
    call = rt.lam.invoke.call_args.kwargs
    assert call["FunctionName"] == "grumpycat-triage" and call["InvocationType"] == "Event"
    assert json.loads(call["Payload"])["fingerprint"] == "fake:1"

    assert router.handler(http_event("/in/fake_in", {"fp": "1"}, {}), CTX)["statusCode"] == 401
    assert router.handler(http_event("/in/fake_in", {"ping": 1}, ok), CTX)["statusCode"] == 204
    assert router.handler(http_event("/in/fake_in", "not json", ok), CTX)["statusCode"] == 400
    assert router.handler(http_event("/in/nope", {}, ok), CTX)["statusCode"] == 404
    assert (
        router.handler(http_event("/in/fake_events", {}, ok), CTX)["statusCode"] == 404
    )  # eventbridge-only
    assert router.handler(http_event("/elsewhere", {}, ok), CTX)["statusCode"] == 404
    assert rt.lam.invoke.call_count == 1


def test_router_eventbridge_path(rt: Runtime) -> None:
    ev = {"source": "aws.ecs", "detail-type": "ECS Task State Change", "detail": {"taskArn": "t1"}}
    assert router.handler(ev, CTX) == {"accepted": "ecs:t1", "input": "fake_events"}
    assert router.handler({"source": "aws.ecs", "detail-type": "Other"}, CTX) == {"accepted": None}
    assert router.handler({"weird": True}, CTX) == {"accepted": None}


# -- triage -------------------------------------------------------------------------------


def event_json(fp: str = "1", **over: Any) -> dict[str, Any]:
    from tests.fakes import FakeInput

    plugin = FakeInput(FakeInput.spec.config_schema(), {"FAKE_TOKEN": "t"})
    ev = plugin.parse({"fp": fp, **over})
    assert ev is not None
    return ev.model_dump(mode="json")


def test_triage_gated_by_default_awaits_approval(rt: Runtime) -> None:
    out = triage.handler(event_json(), CTX)
    assert out["status"] == "awaiting_approval"
    fp = out["fingerprint"]
    assert fp.startswith("fake:1#")  # signature folded in for non-sentry sources
    state = rt.store.get(fp)
    assert state is not None and state.status is IssueStatus.AWAITING_APPROVAL
    assert state.triage is not None and state.triage.confidence == 0.7
    assert state.target is not None and state.target.full_name == "acme/api"
    rt.sfn.start_execution.assert_not_called()
    # duplicate while open
    assert triage.handler(event_json(), CTX)["status"] == "duplicate"


def test_triage_ungated_starts_execution(rt: Runtime) -> None:
    rt.config.policy.gated = False
    out = triage.handler(event_json(), CTX)
    assert out["status"] == "fixing"
    call = rt.sfn.start_execution.call_args.kwargs
    assert call["stateMachineArn"].endswith(":stateMachine:gc")
    payload = json.loads(call["input"])
    assert payload["max_attempts"] == 3 and payload["attempt"] == 0
    assert payload["task"]["kind"] == "fix"
    assert payload["task"]["branch"].startswith("grumpycat/fake-")
    assert "# Production error to fix" in payload["brief_md"]
    state = rt.store.get(out["fingerprint"])
    assert state is not None and state.status is IssueStatus.FIXING
    assert state.execution_arn and state.branch == payload["task"]["branch"]


def test_triage_rca_only_paths(rt: Runtime) -> None:
    rt.config.policy.gated = False
    # no repo mapping
    out = triage.handler(event_json("2", service="mystery"), CTX)
    assert out["status"] == "rca_only" and "no repo mapped" in out["reason"]
    # low confidence (infra-like signature)
    out = triage.handler(event_json("infra-3"), CTX)
    assert out["status"] == "rca_only" and "confidence" in out["reason"]
    # daily cap
    rt.config.policy.prs_per_day = 1
    assert triage.handler(event_json("4"), CTX)["status"] == "fixing"
    out = triage.handler(event_json("5"), CTX)
    assert out["status"] == "rca_only" and "daily PR cap" in out["reason"]


def test_triage_freeze_updates_and_regression_link(rt: Runtime) -> None:
    rt.config.policy.gated = False
    assert triage.handler(event_json("9", transition="recovered"), CTX)["status"] == "ignored"
    first = triage.handler(event_json("9"), CTX)
    fp = first["fingerprint"]
    assert triage.handler(event_json("9", transition="update"), CTX) == {
        "status": "annotated",
        "fingerprint": fp,
        "transition": "update",
    }
    # simulate merged PR, then a regression: previous PR URL flows into the brief
    st = rt.store.get(fp)
    assert st is not None
    rt.store.put(
        st.model_copy(
            update={
                "status": IssueStatus.MERGED,
                "pr_url": HttpUrl("https://github.com/acme/api/pull/3"),
            }
        )
    )
    out = triage.handler(event_json("9", transition="regression"), CTX)
    assert out["status"] == "fixing"
    payload = json.loads(rt.sfn.start_execution.call_args.kwargs["input"])
    assert payload["task"]["brief"]["previous_pr_url"] == "https://github.com/acme/api/pull/3"
    assert "# Regression to fix" in payload["brief_md"]

    rt.config.policy.freeze = True
    assert triage.handler(event_json("10"), CTX) == {"status": "frozen"}


# -- github hook --------------------------------------------------------------------------


def signed(payload: dict[str, Any], kind: str, secret: str = "ghs") -> dict[str, Any]:
    raw = json.dumps(payload)
    sig = "sha256=" + hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return http_event("/hooks/github", raw, {"x-hub-signature-256": sig, "x-github-event": kind})


def tracked_pr(rt: Runtime, number: int = 7, status: IssueStatus = IssueStatus.PR_OPEN) -> None:
    rt.store.put(make_state(status=status, pr_number=number, branch="grumpycat/fake-abc"))


def test_github_hook_rejects_bad_signature_and_answers_ping(rt: Runtime) -> None:
    assert github_hook.handler(signed({}, "ping", "wrong"), CTX)["statusCode"] == 401
    assert json.loads(github_hook.handler(signed({}, "ping"), CTX)["body"]) == {"pong": True}


def test_status_failure_resumes_with_ci_log_via_plugin(rt: Runtime) -> None:
    tracked_pr(rt)
    rt.store.set_wait_token("fake:1", "tok-1")
    payload = {
        "state": "failure",
        "sha": "abc123",
        "context": "buildkite/api",
        "target_url": "https://ci.example.test/b/1",
        "branches": [{"name": "grumpycat/fake-abc"}],
        "repository": {"full_name": "acme/api"},
    }
    r = github_hook.handler(signed(payload, "status"), CTX)
    assert json.loads(r["body"]) == {"resumed": "fake:1", "kind": "ci_failure"}
    call = rt.sfn.send_task_success.call_args.kwargs
    assert call["taskToken"] == "tok-1"
    ev = AwaitedEvent.model_validate_json(call["output"])
    assert (
        ev.ci_failure is not None
        and ev.ci_failure.excerpt == "acme/api@abc123 buildkite/api failed"
    )
    assert rt.store.wait_token("fake:1") is None
    # pending status is ignored; success becomes ci_success
    assert json.loads(
        github_hook.handler(signed({**payload, "state": "pending"}, "status"), CTX)["body"]
    ) == {"ignored": "pending"}
    rt.store.set_wait_token("fake:1", "tok-2")
    github_hook.handler(signed({**payload, "state": "success"}, "status"), CTX)
    assert (
        AwaitedEvent.model_validate_json(rt.sfn.send_task_success.call_args.kwargs["output"]).kind
        == "ci_success"
    )


def test_comment_queues_when_execution_busy_and_respects_allowlist(rt: Runtime) -> None:
    tracked_pr(rt)
    rt.config.reviewer_allowlist = ["cursor[bot]", "alice"]
    base = {
        "action": "created",
        "repository": {"full_name": "acme/api"},
        "issue": {"number": 7, "pull_request": {"url": "x"}},
    }
    by_bob = {**base, "comment": {"body": "nit", "user": {"login": "bob"}}}
    assert json.loads(github_hook.handler(signed(by_bob, "issue_comment"), CTX)["body"]) == {
        "ignored": "actor bob"
    }
    by_self = {**base, "comment": {"body": "I pushed", "user": {"login": "grumpycat[bot]"}}}
    assert "ignored" in json.loads(
        github_hook.handler(signed(by_self, "issue_comment"), CTX)["body"]
    )
    by_alice = {**base, "comment": {"body": "please add a null check", "user": {"login": "alice"}}}
    r = json.loads(github_hook.handler(signed(by_alice, "issue_comment"), CTX)["body"])
    assert r == {"queued": "fake:1", "kind": "comment"}
    rt.sfn.send_task_success.assert_not_called()
    pending = rt.store.pop_pending("fake:1")
    assert (
        pending is not None
        and pending.findings == ["please add a null check"]
        and pending.actor == "alice"
    )


def test_review_comment_includes_path_and_line(rt: Runtime) -> None:
    tracked_pr(rt)
    rt.store.set_wait_token("fake:1", "tok")
    payload = {
        "action": "created",
        "repository": {"full_name": "acme/api"},
        "pull_request": {"number": 7},
        "comment": {
            "body": "use &.",
            "path": "app/x.rb",
            "line": 42,
            "user": {"login": "cursor[bot]"},
        },
    }
    github_hook.handler(signed(payload, "pull_request_review_comment"), CTX)
    ev = AwaitedEvent.model_validate_json(rt.sfn.send_task_success.call_args.kwargs["output"])
    assert ev.findings == ["app/x.rb:42\nuse &."]


def test_review_submitted_and_pr_closed(rt: Runtime) -> None:
    tracked_pr(rt)
    rt.store.set_wait_token("fake:1", "tok")
    review = {
        "action": "submitted",
        "repository": {"full_name": "acme/api"},
        "pull_request": {"number": 7},
        "review": {
            "state": "changes_requested",
            "body": "needs a test",
            "user": {"login": "alice"},
        },
    }
    github_hook.handler(signed(review, "pull_request_review"), CTX)
    assert (
        AwaitedEvent.model_validate_json(rt.sfn.send_task_success.call_args.kwargs["output"]).kind
        == "review"
    )
    approved = {**review, "review": {"state": "approved", "body": "", "user": {"login": "alice"}}}
    assert json.loads(
        github_hook.handler(signed(approved, "pull_request_review"), CTX)["body"]
    ) == {"ignored": "approved"}

    rt.store.set_wait_token("fake:1", "tok2")
    closed = {
        "action": "closed",
        "repository": {"full_name": "acme/api"},
        "pull_request": {"number": 7, "merged": True, "merged_by": {"login": "alice"}},
    }
    github_hook.handler(signed(closed, "pull_request"), CTX)
    ev = AwaitedEvent.model_validate_json(rt.sfn.send_task_success.call_args.kwargs["output"])
    assert ev.kind == "merged" and ev.actor == "alice"


def test_untracked_or_inactive_prs_are_ignored(rt: Runtime) -> None:
    tracked_pr(rt, status=IssueStatus.MERGED)
    payload = {
        "action": "closed",
        "repository": {"full_name": "acme/api"},
        "pull_request": {"number": 7, "merged": False},
    }
    assert json.loads(github_hook.handler(signed(payload, "pull_request"), CTX)["body"]) == {
        "ignored": "no tracked PR"
    }
    check = {
        "action": "completed",
        "repository": {"full_name": "acme/api"},
        "check_run": {
            "conclusion": "failure",
            "head_sha": "s",
            "name": "ci",
            "pull_requests": [{"number": 99}],
        },
    }
    assert json.loads(github_hook.handler(signed(check, "check_run"), CTX)["body"]) == {
        "ignored": "no tracked PR"
    }


# -- lifecycle ----------------------------------------------------------------------------


def test_park_stores_token_or_replays_pending(rt: Runtime) -> None:
    rt.store.put(make_state(status=IssueStatus.PR_OPEN))
    assert lifecycle.handler({"op": "park", "fingerprint": "fake:1", "task_token": "t1"}, CTX) == {
        "parked": "fake:1"
    }
    assert rt.store.wait_token("fake:1") == "t1"
    rt.store.push_pending(
        "fake:1", AwaitedEvent(kind="ci_success", received_at=datetime.now(tz=UTC))
    )
    assert lifecycle.handler({"op": "park", "fingerprint": "fake:1", "task_token": "t2"}, CTX) == {
        "replayed": "ci_success"
    }
    assert rt.sfn.send_task_success.call_args.kwargs["taskToken"] == "t2"


def test_after_run_and_finalize(rt: Runtime) -> None:
    rt.store.put(make_state(status=IssueStatus.FIXING, branch="grumpycat/fake-abc"))
    outcome = FixOutcome(
        status="pr_open", pr_number=12, pr_url="https://github.com/acme/api/pull/12", cost_usd=1.25
    )  # type: ignore[arg-type]
    r = lifecycle.handler(
        {
            "op": "after_run",
            "fingerprint": "fake:1",
            "outcome": outcome.model_dump(mode="json"),
            "attempt": 1,
        },
        CTX,
    )
    assert r["status"] == "pr_open"
    st = rt.store.get("fake:1")
    assert st is not None and st.pr_number == 12 and st.cost_usd == 1.25 and st.attempts == 1
    assert rt.store.get_by_pr("acme/api", 12) is not None

    # first push with no PR yet -> PR_OPEN (the GitHub output opens the draft PR on that
    # transition); a later push on a tracked PR -> SHEPHERDING
    rt.store.put(make_state(status=IssueStatus.FIXING, branch="grumpycat/fake-abc"))
    pushed = FixOutcome(status="pushed", branch="grumpycat/fake-abc", summary="v1")
    lifecycle.handler(
        {"op": "after_run", "fingerprint": "fake:1", "outcome": pushed.model_dump(mode="json")},
        CTX,
    )
    assert rt.store.get("fake:1").status is IssueStatus.PR_OPEN  # type: ignore[union-attr]
    st = rt.store.get("fake:1")
    assert st is not None
    rt.store.put(st.model_copy(update={"pr_number": 12}))
    lifecycle.handler(
        {"op": "after_run", "fingerprint": "fake:1", "outcome": pushed.model_dump(mode="json")},
        CTX,
    )
    assert rt.store.get("fake:1").status is IssueStatus.SHEPHERDING  # type: ignore[union-attr]

    declined = FixOutcome(status="declined", summary="could not reproduce")
    lifecycle.handler(
        {
            "op": "after_run",
            "fingerprint": "fake:1",
            "outcome": declined.model_dump(mode="json"),
            "attempt": 1,
        },
        CTX,
    )
    assert rt.store.get("fake:1").status is IssueStatus.RCA_ONLY  # type: ignore[union-attr]

    rt.store.set_wait_token("fake:1", "t")
    assert (
        lifecycle.handler(
            {
                "op": "finalize",
                "fingerprint": "fake:1",
                "outcome": "needs_human",
                "reason": "budget",
            },
            CTX,
        )["status"]
        == "needs_human"
    )
    st = rt.store.get("fake:1")
    assert st is not None and st.rationale == "budget" and rt.store.wait_token("fake:1") is None
    with pytest.raises(ValueError, match="unknown op"):
        lifecycle.handler({"op": "dance", "fingerprint": "fake:1"}, CTX)
