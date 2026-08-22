"""DynamoDB-backed issue state. One table, two item shapes:

  pk = "ISSUE#<fingerprint>"      the `IssueState` document (+ `wait_token` while parked)
  pk = "PR#<owner/name>#<number>"  → fingerprint, so GitHub events can find the issue

While the execution is parked waiting for GitHub, the issue row carries `wait_token`. Events
that arrive while a task is *running* (no token yet) queue in `pending_events` and are
replayed by the park Lambda, so nothing is lost between iterations.

Invariants enforced here, not in callers:
  * one *open* issue per fingerprint (`claim` is a conditional put);
  * a closed-unmerged PR blocks a re-attempt for `cooldown_hours`;
  * `prs_per_day` is counted per repo from the `opened_on` attribute.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from grumpycat.core.models import AwaitedEvent, IssueState, IssueStatus

TERMINAL = {IssueStatus.MERGED, IssueStatus.CLOSED, IssueStatus.RCA_ONLY, IssueStatus.NEEDS_HUMAN}
# Statuses that mean an engine run was started for this issue; these count against prs_per_day.
STARTED = {
    IssueStatus.FIXING,
    IssueStatus.PR_OPEN,
    IssueStatus.GROOMING,
    IssueStatus.READY,
    IssueStatus.MERGED,
    IssueStatus.CLOSED,
    IssueStatus.NEEDS_HUMAN,
}


class AlreadyOpen(Exception):  # noqa: N818 - it's a state, not an error condition
    """Another run owns this fingerprint right now (or the cool-down is still in force)."""

    def __init__(self, existing: IssueState) -> None:
        super().__init__(existing.fingerprint)
        self.existing = existing


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _dump(state: IssueState) -> dict[str, Any]:
    # DynamoDB refuses Python floats; round-trip through JSON with Decimal parsing.
    return dict(json.loads(state.model_dump_json(), parse_float=Decimal))


class IssueStore:
    def __init__(self, table_name: str | None = None, *, resource: Any = None) -> None:
        name = table_name or os.environ["GRUMPYCAT_TABLE"]
        self.table = (resource or boto3.resource("dynamodb")).Table(name)

    # -- reads ----------------------------------------------------------------------------

    def get(self, fingerprint: str) -> IssueState | None:
        item = self.table.get_item(Key={"pk": f"ISSUE#{fingerprint}"}).get("Item")
        return IssueState.model_validate(item["state"]) if item else None

    def get_by_pr(self, repo: str, number: int) -> IssueState | None:
        item = self.table.get_item(Key={"pk": f"PR#{repo}#{number}"}).get("Item")
        return self.get(str(item["fingerprint"])) if item else None

    def get_by_branch(self, repo: str, branch: str) -> IssueState | None:
        """Commit `status` events carry branches, not PR numbers."""
        resp = self.table.query(
            IndexName="branch",
            KeyConditionExpression="branch = :b",
            ExpressionAttributeValues={":b": f"{repo}#{branch}"},
            Limit=1,
        )
        items = resp.get("Items") or []
        return IssueState.model_validate(items[0]["state"]) if items else None

    def wait_token(self, fingerprint: str) -> str | None:
        item = self.table.get_item(
            Key={"pk": f"ISSUE#{fingerprint}"}, ProjectionExpression="wait_token"
        ).get("Item")
        token = (item or {}).get("wait_token")
        return str(token) if token else None

    def opened_today(self, repo: str) -> int:
        today = _now().date().isoformat()
        resp = self.table.query(
            IndexName="opened_on",
            KeyConditionExpression="opened_on = :d",
            ExpressionAttributeValues={":d": f"{repo}#{today}"},
            Select="COUNT",
        )
        return int(resp.get("Count", 0))

    # -- writes ---------------------------------------------------------------------------

    def claim(self, state: IssueState, *, cooldown_hours: int) -> IssueState:
        """Create the issue row, or raise `AlreadyOpen` if one is live or cooling down.

        A terminal row older than the cool-down is overwritten; its history lives in the
        PR and in Step Functions, not here.
        """
        existing = self.get(state.fingerprint)
        if existing is not None:
            live = existing.status not in TERMINAL
            cooling = (
                existing.status == IssueStatus.CLOSED
                and existing.updated_at > _now() - timedelta(hours=cooldown_hours)
            )
            if live or cooling:
                raise AlreadyOpen(existing)
        try:
            self.table.put_item(
                Item=self._item(state),
                ConditionExpression="attribute_not_exists(pk) OR #s IN (:t1, :t2, :t3, :t4)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":t1": IssueStatus.MERGED,
                    ":t2": IssueStatus.CLOSED,
                    ":t3": IssueStatus.RCA_ONLY,
                    ":t4": IssueStatus.NEEDS_HUMAN,
                },
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise AlreadyOpen(existing or state) from e  # lost a race
            raise
        return state

    def put(self, state: IssueState) -> IssueState:
        """Write the state document without touching `wait_token` / `pending_events`."""
        state = state.model_copy(update={"updated_at": _now()})
        item = self._item(state)
        names = {f"#{k}": k for k in item if k != "pk"}
        values = {f":{k}": v for k, v in item.items() if k != "pk"}
        self.table.update_item(
            Key={"pk": item["pk"]},
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in item if k != "pk"),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        if state.pr_number is not None and state.target is not None:
            self.table.put_item(
                Item={
                    "pk": f"PR#{state.target.full_name}#{state.pr_number}",
                    "fingerprint": state.fingerprint,
                }
            )
        return state

    def set_wait_token(self, fingerprint: str, token: str | None) -> None:
        if token is None:
            self.table.update_item(
                Key={"pk": f"ISSUE#{fingerprint}"}, UpdateExpression="REMOVE wait_token"
            )
        else:
            self.table.update_item(
                Key={"pk": f"ISSUE#{fingerprint}"},
                UpdateExpression="SET wait_token = :t",
                ExpressionAttributeValues={":t": token},
            )

    def push_pending(self, fingerprint: str, event: AwaitedEvent) -> None:
        self.table.update_item(
            Key={"pk": f"ISSUE#{fingerprint}"},
            UpdateExpression=(
                "SET pending_events = list_append(if_not_exists(pending_events, :e), :n)"
            ),
            ExpressionAttributeValues={
                ":e": [],
                ":n": [json.loads(event.model_dump_json(), parse_float=Decimal)],
            },
        )

    def pop_pending(self, fingerprint: str) -> AwaitedEvent | None:
        item = self.table.get_item(
            Key={"pk": f"ISSUE#{fingerprint}"}, ProjectionExpression="pending_events"
        ).get("Item")
        raw: Any = (item or {}).get("pending_events") or []
        pending = list(raw)
        if not pending:
            return None
        self.table.update_item(
            Key={"pk": f"ISSUE#{fingerprint}"}, UpdateExpression="REMOVE pending_events[0]"
        )
        return AwaitedEvent.model_validate(pending[0])

    def _item(self, state: IssueState) -> dict[str, Any]:
        item: dict[str, Any] = {
            "pk": f"ISSUE#{state.fingerprint}",
            "status": state.status,
            "state": _dump(state),
            "updated_at": state.updated_at.isoformat(),
        }
        if state.target is not None:
            repo = state.target.full_name
            if state.branch:
                item["branch"] = f"{repo}#{state.branch}"
            if state.status in STARTED:
                item["opened_on"] = f"{repo}#{state.created_at.date().isoformat()}"
        return item
