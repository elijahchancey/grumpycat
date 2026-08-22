"""Small Lambdas the state machine calls between worker runs.

park      `.waitForTaskToken` step: stores the token so the GitHub hook can resume us, or
          replays an event that arrived while the worker was running.
after_run records the worker's `FixOutcome` on the issue and notifies outputs.
finalize  terminal transitions (ready / needs_human / rca_only / merged / closed).
"""

from __future__ import annotations

from typing import Any

from grumpycat.core.models import Brief, FixOutcome, IssueState, IssueStatus
from grumpycat.handlers.runtime import Runtime, logger, runtime


def _notify(
    rt: Runtime, state: IssueState, previous: IssueState | None, brief: Brief | None
) -> IssueState:
    for name, output in rt.registry.outputs.items():
        try:
            state = output.on_transition(state, previous, brief)
        except Exception:
            logger.exception("output failed", output=name, fingerprint=state.fingerprint)
    return state


def park(rt: Runtime, fingerprint: str, token: str) -> dict[str, Any]:
    pending = rt.store.pop_pending(fingerprint)
    if pending is not None:
        rt.sfn.send_task_success(taskToken=token, output=pending.model_dump_json())
        logger.info("replayed pending event", fingerprint=fingerprint, kind=pending.kind)
        return {"replayed": pending.kind}
    rt.store.set_wait_token(fingerprint, token)
    return {"parked": fingerprint}


def after_run(
    rt: Runtime, fingerprint: str, outcome: FixOutcome, brief: Brief | None, attempt: int
) -> dict[str, Any]:
    state = rt.store.get(fingerprint)
    if state is None:
        logger.error("after_run for unknown issue", fingerprint=fingerprint)
        return {"unknown": fingerprint}
    previous = state
    update: dict[str, Any] = {
        "attempts": attempt,
        "cost_usd": round(state.cost_usd + (outcome.cost_usd or 0.0), 4),
    }
    if outcome.status == "pr_open":
        update |= {
            "status": IssueStatus.PR_OPEN,
            "pr_number": outcome.pr_number,
            "pr_url": outcome.pr_url,
            "branch": outcome.branch or state.branch,
        }
    elif outcome.status == "pushed":
        # First push: the GitHub output opens the draft PR on the PR_OPEN transition.
        update["status"] = (
            IssueStatus.SHEPHERDING if state.pr_number is not None else IssueStatus.PR_OPEN
        )
        update["branch"] = outcome.branch or state.branch
    elif outcome.status == "declined":
        update |= {"status": IssueStatus.RCA_ONLY, "rationale": outcome.summary}
    else:
        update |= {"status": IssueStatus.NEEDS_HUMAN, "rationale": outcome.summary}
    state = rt.store.put(_notify(rt, state.model_copy(update=update), previous, brief))
    return {"status": state.status, "fingerprint": fingerprint}


def finalize(rt: Runtime, fingerprint: str, outcome: str, reason: str | None) -> dict[str, Any]:
    state = rt.store.get(fingerprint)
    if state is None:
        return {"unknown": fingerprint}
    status = {
        "ready": IssueStatus.READY,
        "needs_human": IssueStatus.NEEDS_HUMAN,
        "rca_only": IssueStatus.RCA_ONLY,
        "merged": IssueStatus.MERGED,
        "closed": IssueStatus.CLOSED,
    }[outcome]
    previous = state
    state = state.model_copy(update={"status": status, "rationale": reason or state.rationale})
    rt.store.set_wait_token(fingerprint, None)
    state = rt.store.put(_notify(rt, state, previous, None))
    logger.info("finalized", fingerprint=fingerprint, status=status)
    return {"status": status, "fingerprint": fingerprint}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """One Lambda, dispatched on `event["op"]` — keeps the module's Lambda count down."""
    rt = runtime()
    op = event.get("op")
    fp = str(event["fingerprint"])
    if op == "park":
        return park(rt, fp, str(event["task_token"]))
    if op == "after_run":
        brief = Brief.model_validate(event["brief"]) if event.get("brief") else None
        return after_run(
            rt, fp, FixOutcome.model_validate(event["outcome"]), brief, int(event.get("attempt", 1))
        )
    if op == "finalize":
        return finalize(rt, fp, str(event["outcome"]), event.get("reason"))
    msg = f"unknown op {op!r}"
    raise ValueError(msg)
