from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from grumpycat.core.models import AwaitedEvent, IssueStatus
from grumpycat.core.store import AlreadyOpen, IssueStore
from tests.conftest import make_state


def test_claim_then_get_roundtrip(store: IssueStore) -> None:
    s = store.claim(make_state(), cooldown_hours=72)
    got = store.get("fake:1")
    assert got == s
    assert store.get("nope") is None


def test_claim_rejects_live_issue_and_cooldown(store: IssueStore) -> None:
    store.claim(make_state(status=IssueStatus.PR_OPEN), cooldown_hours=72)
    with pytest.raises(AlreadyOpen) as e:
        store.claim(make_state(), cooldown_hours=72)
    assert e.value.existing.status is IssueStatus.PR_OPEN

    recent = datetime.now(tz=UTC) - timedelta(hours=1)
    store.put(make_state(status=IssueStatus.CLOSED, updated_at=recent))
    with pytest.raises(AlreadyOpen):
        store.claim(make_state(), cooldown_hours=72)
    # cool-down elapsed -> re-claimable
    store.put(make_state(status=IssueStatus.CLOSED))
    store.table.update_item(
        Key={"pk": "ISSUE#fake:1"},
        UpdateExpression="SET #s.updated_at = :t",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":t": (recent - timedelta(days=10)).isoformat()},
    )
    store.claim(make_state(), cooldown_hours=72)


@pytest.mark.parametrize(
    "terminal", [IssueStatus.MERGED, IssueStatus.RCA_ONLY, IssueStatus.NEEDS_HUMAN]
)
def test_terminal_rows_can_be_reclaimed(store: IssueStore, terminal: IssueStatus) -> None:
    store.put(make_state(status=terminal))
    store.claim(make_state(), cooldown_hours=72)
    assert store.get("fake:1").status is IssueStatus.TRIAGED  # type: ignore[union-attr]


def test_put_preserves_token_and_pending_and_links_pr(store: IssueStore) -> None:
    store.claim(make_state(), cooldown_hours=72)
    store.set_wait_token("fake:1", "tok")
    ev = AwaitedEvent(kind="comment", findings=["x"], received_at=datetime.now(tz=UTC))
    store.push_pending("fake:1", ev)
    store.put(make_state(status=IssueStatus.PR_OPEN, pr_number=7, branch="grumpycat/fake-abc"))
    assert store.wait_token("fake:1") == "tok"
    assert store.pop_pending("fake:1") == ev
    assert store.pop_pending("fake:1") is None
    assert store.get_by_pr("acme/api", 7).fingerprint == "fake:1"  # type: ignore[union-attr]
    assert store.get_by_branch("acme/api", "grumpycat/fake-abc").pr_number == 7  # type: ignore[union-attr]
    assert store.get_by_branch("acme/api", "grumpycat/other") is None
    store.set_wait_token("fake:1", None)
    assert store.wait_token("fake:1") is None


def test_opened_today_counts_only_started_issues(store: IssueStore) -> None:
    store.put(make_state(fingerprint="a", status=IssueStatus.TRIAGED))
    store.put(make_state(fingerprint="b", status=IssueStatus.AWAITING_APPROVAL))
    store.put(make_state(fingerprint="r", status=IssueStatus.RCA_ONLY))
    assert store.opened_today("acme/api") == 0
    store.put(make_state(fingerprint="c", status=IssueStatus.FIXING))
    store.put(make_state(fingerprint="d", status=IssueStatus.PR_OPEN))
    assert store.opened_today("acme/api") == 2
    assert store.opened_today("acme/frontend") == 0
