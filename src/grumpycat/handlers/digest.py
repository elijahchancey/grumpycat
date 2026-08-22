"""Scheduled digest: what Grumpycat did in the last `hours`, per repo, posted to Slack.

Invoked by an EventBridge schedule with `{"hours": 24}` (default). Reads the issues table
(scan filtered on `updated_at`; the table is small by construction — one row per fingerprint)
and posts one message to the Slack output's channel. No Slack output configured → logs only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from grumpycat.core.models import IssueState, IssueStatus
from grumpycat.handlers.runtime import Runtime, logger, runtime

ORDER = [
    IssueStatus.MERGED,
    IssueStatus.READY,
    IssueStatus.PR_OPEN,
    IssueStatus.GROOMING,
    IssueStatus.FIXING,
    IssueStatus.AWAITING_APPROVAL,
    IssueStatus.NEEDS_HUMAN,
    IssueStatus.RCA_ONLY,
    IssueStatus.CLOSED,
    IssueStatus.TRIAGED,
]


def recent_states(rt: Runtime, since: datetime) -> list[IssueState]:
    table = rt.store.table
    out: list[IssueState] = []
    kwargs: dict[str, Any] = {
        "FilterExpression": "begins_with(pk, :p) AND updated_at >= :s",
        "ExpressionAttributeValues": {":p": "ISSUE#", ":s": since.isoformat()},
        "ProjectionExpression": "#st",
        "ExpressionAttributeNames": {"#st": "state"},
    }
    while True:
        resp = table.scan(**kwargs)
        out.extend(IssueState.model_validate(i["state"]) for i in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return out
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def render(states: list[IssueState], hours: int, client: str) -> str:
    if not states:
        return f"*grumpycat · {client}* — nothing happened in the last {hours}h."
    by_repo: dict[str, list[IssueState]] = defaultdict(list)
    for s in states:
        by_repo[s.target.full_name if s.target else "(unmapped)"].append(s)
    total_cost = sum(s.cost_usd for s in states)
    counts: Counter[IssueStatus] = Counter(s.status for s in states)
    head = (
        f"*grumpycat · {client}* — last {hours}h: {len(states)} issue(s), "
        f"{counts[IssueStatus.MERGED]} merged, {counts[IssueStatus.READY]} ready, "
        f"{counts[IssueStatus.NEEDS_HUMAN]} need a human, {counts[IssueStatus.RCA_ONLY]} RCA-only, "
        f"${total_cost:.2f} in engine spend"
    )
    lines = [head]
    for repo in sorted(by_repo):
        items = sorted(by_repo[repo], key=lambda s: (ORDER.index(s.status), s.updated_at))
        lines.append(f"\n*{repo}* ({len(items)})")
        for s in items[:15]:
            link = (
                f"<{s.pr_url}|PR #{s.pr_number}>"
                if s.pr_url
                else (f"<{s.event.url}|source>" if s.event.url else "")
            )
            cost = f" · ${s.cost_usd:.2f}" if s.cost_usd else ""
            why = (
                f" — _{s.rationale}_"
                if s.rationale
                and s.status in {IssueStatus.NEEDS_HUMAN, IssueStatus.RCA_ONLY, IssueStatus.CLOSED}
                else ""
            )
            lines.append(f"• `{s.status}` {s.event.title[:80]} {link}{cost}{why}".rstrip())
        if len(items) > 15:
            lines.append(f"• … {len(items) - 15} more")
    return "\n".join(lines)


def run(rt: Runtime, hours: int) -> dict[str, Any]:
    since = datetime.now(tz=UTC) - timedelta(hours=hours)
    states = recent_states(rt, since)
    text = render(states, hours, rt.config.client)
    slack = rt.registry.outputs.get("slack")
    posted = False
    if slack is not None:
        channel = getattr(slack.config, "channel", None)
        post = getattr(slack, "_post", None)
        if channel and callable(post):
            post(channel, text)
            posted = True
    logger.info("digest", issues=len(states), posted=posted)
    return {"issues": len(states), "posted": posted, "text": text if not posted else None}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return run(runtime(), int((event or {}).get("hours") or 24))
