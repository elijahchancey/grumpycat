from __future__ import annotations

from grumpycat.core.brief import render_brief
from grumpycat.core.config import load_config
from grumpycat.core.fingerprint import branch_name, final_fingerprint, short_id
from grumpycat.core.models import (
    Brief,
    CIFailure,
    Evidence,
    Exception_,
    RepoTarget,
    Severity,
    StackFrame,
    Transition,
)
from grumpycat.core.triage import confidence, severity, triage
from tests.conftest import MINIMAL_YAML, make_event

CFG = load_config(MINIMAL_YAML)


def exc(in_app: bool = True, value: str = "undefined method `email' for nil") -> Exception_:
    return Exception_(
        type="NoMethodError",
        value=value,
        frames=[StackFrame(filename="app/x.rb", lineno=1, in_app=in_app)],
    )


def test_confidence_ladder() -> None:
    assert confidence(make_event(), Evidence())[0] == 0.2
    assert confidence(make_event(), Evidence(signature="X: y"))[0] == 0.4
    assert confidence(make_event(), Evidence(exception=exc(in_app=False)))[0] == 0.5
    assert confidence(make_event(), Evidence(exception=exc()))[0] == 0.7
    reg = make_event(transition=Transition.REGRESSION)
    assert confidence(reg, Evidence(exception=exc()))[0] == 0.8


def test_infra_signatures_lower_confidence_unless_author_points_at_code() -> None:
    infra = Evidence(signature="ThrottlingException: Rate exceeded")
    c, why = confidence(make_event(), infra)
    assert c == 0.05 and any("infrastructural" in w for w in why)
    hinted = infra.model_copy(update={"message": "the SDK gave up and nothing will retry it"})
    assert confidence(make_event(), hinted)[0] == 0.2


def test_severity_and_paging() -> None:
    assert severity(make_event(), Evidence())[0] is Severity.LOW
    assert severity(make_event(), Evidence(user_count=3))[0] is Severity.MEDIUM
    assert severity(make_event(), Evidence(user_count=25))[0] is Severity.HIGH
    assert severity(make_event(source="datadog"), Evidence())[0] is Severity.HIGH
    assert severity(make_event(), Evidence(event_count=5000))[0] is Severity.CRITICAL
    assert severity(make_event(tags={"level": "fatal"}), Evidence())[0] is Severity.CRITICAL

    t = triage(make_event(), Evidence(user_count=57, exception=exc()), CFG)
    assert t.page is True and t.severity is Severity.HIGH and t.confidence == 0.7
    assert "page" in t.rationale
    staging = triage(make_event(env="staging"), Evidence(user_count=500), CFG)
    assert staging.page is False
    assert triage(
        make_event(env="prod-api"), Evidence(tags=None) if False else Evidence(user_count=50), CFG
    ).page


def test_fingerprint_helpers() -> None:
    ev = make_event(fingerprint="datadog:1:scope")
    assert final_fingerprint(ev, Evidence()) == "datadog:1:scope"
    fp = final_fingerprint(ev, Evidence(signature="A: b"))
    assert fp.startswith("datadog:1:scope#") and len(fp.split("#")[1]) == 12
    sentry = make_event(fingerprint="sentry:acme:1")
    assert final_fingerprint(sentry, Evidence(signature="A: b")) == "sentry:acme:1"
    assert branch_name(fp).startswith("grumpycat/datadog-") and len(short_id(fp)) == 10


def test_render_brief_fix_and_groom() -> None:
    ev = make_event(fingerprint="sentry:acme:1", url="https://acme.sentry.io/issues/1/")
    evidence = Evidence(
        exception=exc(),
        deployed_sha="abc1234",
        user_count=57,
        event_count=412,
        routing_hints=["culprit: app/x.rb in y"],
        message="TRUST & SAFETY: nothing will retry it",
        sample_logs=["line1", "line2"],
        request={"method": "POST"},
    )
    t = triage(ev, evidence, CFG)
    brief = Brief(
        event=ev,
        evidence=evidence,
        triage=t,
        target=RepoTarget(full_name="acme/api", engine="claude"),
        previous_pr_url="https://github.com/acme/api/pull/1",  # type: ignore[arg-type]
    )
    md = render_brief(brief)
    for needle in (
        "# Production error to fix",
        "`abc1234` (this checkout)",
        "Previous fix for this issue: https://github.com/acme/api/pull/1",
        "## Where to look",
        "NoMethodError: undefined method",
        "* app/x.rb:1",
        "## Alert message (verbatim)",
        "## Sample logs / stack",
        "## Request (scrubbed)",
        "Do not run the full test suite",
        "GRUMPYCAT_REPORT.md",
    ):
        assert needle in md, needle
    assert "Feedback to address" not in md

    shep = render_brief(
        brief,
        ci_failure=CIFailure(
            excerpt="rspec failed\n  x_spec.rb:3",
            job_name="rspec",
            build_url="https://ci.example.test/1",
        ),  # type: ignore[arg-type]
        findings=["please use safe navigation here"],
    )
    assert "## Feedback to address" in shep
    assert "### CI failure — rspec (https://ci.example.test/1)" in shep
    assert "### Review comment 1" in shep
    assert "without weakening the regression test" in shep
    assert "Do not run the full test suite" not in shep


def test_render_brief_truncates_long_blocks() -> None:
    ev = make_event()
    evidence = Evidence(sample_logs=["x" * 10000])
    brief = Brief(
        event=ev,
        evidence=evidence,
        triage=triage(ev, evidence, CFG),
        target=RepoTarget(full_name="a/b", engine="codex"),
    )
    assert "…(truncated)" in render_brief(brief)
