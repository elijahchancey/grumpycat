"""Enrich, dedupe, score, apply policy, and start (or park) the per-issue state machine.

Outcomes, in order of evaluation:

  frozen            policy.freeze — nothing recorded
  annotated         RECOVERED / UPDATE for an issue we already track
  ignored           RECOVERED / UPDATE for an issue we don't track
  duplicate         an open issue (or a cooling-down one) owns this fingerprint
  rca_only          no repo mapping, confidence below threshold, or daily PR cap reached
  awaiting_approval policy.gated — Slack approval starts the execution later
  fixing            execution started
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from grumpycat.core.brief import render_brief
from grumpycat.core.fingerprint import branch_name, final_fingerprint, short_id
from grumpycat.core.models import (
    Brief,
    ErrorEvent,
    Evidence,
    IssueState,
    IssueStatus,
    TaskKind,
    Transition,
    Triage,
    WorkerTask,
)
from grumpycat.core.store import AlreadyOpen
from grumpycat.core.triage import triage as score
from grumpycat.handlers.runtime import Runtime, logger, runtime


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _notify(
    rt: Runtime, state: IssueState, previous: IssueState | None, brief: Brief | None
) -> IssueState:
    for name, output in rt.registry.outputs.items():
        try:
            state = output.on_transition(state, previous, brief)
        except Exception:
            logger.exception("output failed", output=name, fingerprint=state.fingerprint)
    return state


def _rca_only(rt: Runtime, state: IssueState, brief: Brief | None, reason: str) -> dict[str, Any]:
    state = state.model_copy(update={"status": IssueStatus.RCA_ONLY, "rationale": reason})
    state = rt.store.put(_notify(rt, state, None, brief))
    return {"status": "rca_only", "fingerprint": state.fingerprint, "reason": reason}


def start_fix(rt: Runtime, state: IssueState, brief: Brief) -> IssueState:
    """Start the Step Functions execution for a triaged issue. Shared with the Slack approval."""
    if state.target is None:  # invariant: callers resolve the repo before starting
        msg = f"start_fix called without a target for {state.fingerprint}"
        raise RuntimeError(msg)
    branch = branch_name(state.fingerprint)
    task = WorkerTask(kind=TaskKind.FIX, brief=brief, branch=branch)
    name = f"{short_id(state.fingerprint)}-{int(_now().timestamp())}"
    resp = rt.sfn.start_execution(
        stateMachineArn=rt.state_machine_arn,
        name=name,
        input=_execution_input(rt, state, task),
    )
    previous = state
    state = state.model_copy(
        update={
            "status": IssueStatus.FIXING,
            "branch": branch,
            "execution_arn": resp["executionArn"],
        }
    )
    state = rt.store.put(_notify(rt, state, previous, brief))
    logger.info("execution started", fingerprint=state.fingerprint, execution=resp["executionArn"])
    return state


def _execution_input(rt: Runtime, state: IssueState, task: WorkerTask) -> str:
    import json

    return json.dumps(
        {
            "fingerprint": state.fingerprint,
            "max_attempts": rt.config.policy.max_attempts,
            "attempt": 0,
            "task": task.model_dump(mode="json"),
            "brief_md": render_brief(task.brief),
        }
    )


def _handle_update(rt: Runtime, event: ErrorEvent, evidence: Evidence) -> dict[str, Any]:
    fp = final_fingerprint(event, evidence)
    existing = rt.store.get(fp) or rt.store.get(event.fingerprint)
    if existing is None:
        return {"status": "ignored", "fingerprint": fp}
    # Outputs see the same state twice with a new event attached; Slack replies in-thread.
    updated = existing.model_copy(update={"event": event})
    rt.store.put(_notify(rt, updated, existing, None))
    return {"status": "annotated", "fingerprint": fp, "transition": event.transition}


def process(rt: Runtime, event: ErrorEvent) -> dict[str, Any]:
    policy = rt.config.policy
    if policy.freeze:
        logger.info("frozen; ignoring", fingerprint=event.fingerprint)
        return {"status": "frozen"}

    plugin = rt.registry.inputs[event.source]
    try:
        evidence = plugin.enrich(event)
    except Exception:
        logger.exception("enrich failed; continuing with the bare event", source=event.source)
        evidence = Evidence()

    if event.transition in {Transition.RECOVERED, Transition.UPDATE}:
        return _handle_update(rt, event, evidence)

    fp = final_fingerprint(event, evidence)
    event = event.model_copy(update={"fingerprint": fp})
    tri: Triage = score(event, evidence, rt.config)
    now = _now()
    resolved = rt.config.resolve_repo(event.service)
    target = resolved[1].to_target(resolved[0]) if resolved else None
    state = IssueState(
        fingerprint=fp,
        status=IssueStatus.TRIAGED,
        event=event,
        triage=tri,
        target=target,
        created_at=now,
        updated_at=now,
    )

    previous_pr = None
    try:
        prior = rt.store.get(fp)
        if prior is not None and prior.pr_url is not None:
            previous_pr = prior.pr_url
        state = rt.store.claim(state, cooldown_hours=policy.cooldown_hours)
    except AlreadyOpen as dup:
        logger.info("duplicate", fingerprint=fp, status=dup.existing.status)
        return {"status": "duplicate", "fingerprint": fp, "existing": dup.existing.status}

    if target is None:
        return _rca_only(rt, state, None, f"no repo mapped for service {event.service!r}")
    brief = Brief(
        event=event, evidence=evidence, triage=tri, target=target, previous_pr_url=previous_pr
    )
    if tri.confidence < policy.confidence_min:
        return _rca_only(
            rt,
            state,
            brief,
            f"confidence {tri.confidence:.2f} < {policy.confidence_min:.2f}: {tri.rationale}",
        )
    if policy.prs_per_day and rt.store.opened_today(target.full_name) >= policy.prs_per_day:
        return _rca_only(
            rt, state, brief, f"daily PR cap ({policy.prs_per_day}) reached for {target.full_name}"
        )

    state = state.model_copy(update={"brief": brief})
    if policy.gated:
        state = state.model_copy(update={"status": IssueStatus.AWAITING_APPROVAL})
        state = rt.store.put(_notify(rt, state, None, brief))
        return {"status": "awaiting_approval", "fingerprint": fp}

    state = start_fix(rt, state, brief)
    return {"status": "fixing", "fingerprint": fp, "execution": state.execution_arn}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return process(runtime(), ErrorEvent.model_validate(event))
