from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import EntryPoint
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from grumpycat.core.config import load_config
from grumpycat.core.models import ErrorEvent, IssueState, IssueStatus, RepoTarget, Transition
from grumpycat.core.store import IssueStore
from grumpycat.handlers import runtime as runtime_mod
from grumpycat.plugins import Registry, registry
from grumpycat.plugins.spec import PluginKind

MINIMAL_YAML = """
client: acme
inputs:
  fake_in: {}
outputs:
  fake_out: {}
repos:
  acme/api:
    engine: fake_engine
    default_branch: master
    ci_pipeline: acme/api
    services: [api, api-worker]
  acme/frontend:
    engine: fake_engine
"""

TABLE = "grumpycat-test"

Register = Callable[[PluginKind, dict[str, Any]], None]


@pytest.fixture
def fake_entry_points(monkeypatch: pytest.MonkeyPatch) -> Register:
    """Register in-memory plugin classes as if they were installed entry points.

    Usage: fake_entry_points(PluginKind.INPUT, {"fake_in": FakeInput})
    """
    table: dict[PluginKind, dict[str, EntryPoint]] = {k: {} for k in PluginKind}

    def fake_discover(kind: PluginKind) -> dict[str, EntryPoint]:
        return dict(table[kind])

    monkeypatch.setattr(registry, "discover", fake_discover)

    def register(kind: PluginKind, classes: dict[str, Any]) -> None:
        for name, cls in classes.items():
            # EntryPoint.load() imports by dotted path; classes must be module-level.
            table[kind][name] = EntryPoint(
                name=name, value=f"{cls.__module__}:{cls.__qualname__}", group=registry.GROUPS[kind]
            )

    return register


# -- AWS-backed fixtures (moto) ------------------------------------------------------------


def create_table(ddb: Any) -> Any:
    return ddb.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "branch", "AttributeType": "S"},
            {"AttributeName": "opened_on", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "branch",
                "KeySchema": [{"AttributeName": "branch", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "opened_on",
                "KeySchema": [{"AttributeName": "opened_on", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
        ],
    )


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        yield boto3


@pytest.fixture
def store(aws: Any) -> IssueStore:
    ddb = aws.resource("dynamodb")
    create_table(ddb)
    return IssueStore(TABLE, resource=ddb)


def make_event(**over: Any) -> ErrorEvent:
    base: dict[str, Any] = {
        "source": "fake_in",
        "fingerprint": "fake:1",
        "transition": Transition.NEW,
        "service": "api",
        "env": "prod",
        "title": "NoMethodError: boom",
        "occurred_at": datetime(2026, 8, 21, 3, 14, tzinfo=UTC),
        "tags": {"level": "error"},
    }
    return ErrorEvent(**(base | over))


def make_state(**over: Any) -> IssueState:
    now = datetime.now(tz=UTC)
    base: dict[str, Any] = {
        "fingerprint": "fake:1",
        "status": IssueStatus.TRIAGED,
        "event": make_event(),
        "target": RepoTarget(full_name="acme/api", engine="fake_engine", default_branch="master"),
        "created_at": now,
        "updated_at": now,
    }
    return IssueState(**(base | over))


@pytest.fixture
def rt(
    store: IssueStore, fake_entry_points: Register, monkeypatch: pytest.MonkeyPatch
) -> runtime_mod.Runtime:
    """A Runtime with fake plugins, a moto table and mocked SFN/Lambda clients."""
    from tests import fakes

    fake_entry_points(
        PluginKind.INPUT, {"fake_in": fakes.FakeInput, "fake_events": fakes.FakeEventInput}
    )
    fake_entry_points(PluginKind.ENGINE, {"fake_engine": fakes.FakeEngine})
    fake_entry_points(PluginKind.OUTPUT, {"fake_out": fakes.FakeOutput})
    fake_entry_points(PluginKind.CI, {"fake_ci": fakes.FakeCI})
    monkeypatch.setenv("GRUMPYCAT_STATE_MACHINE", "arn:aws:states:us-east-1:123:stateMachine:gc")
    monkeypatch.setenv("GRUMPYCAT_TRIAGE_FUNCTION", "grumpycat-triage")
    config = load_config(
        MINIMAL_YAML.replace("fake_in: {}", "fake_in: {}\n  fake_events: {}")
        + "ci:\n  provider: fake_ci\n"
    )
    secrets = {"FAKE_TOKEN": "t", "CI_TOKEN": "c", "GITHUB_WEBHOOK_SECRET": "ghs"}
    sfn = MagicMock()
    sfn.start_execution.return_value = {
        "executionArn": "arn:aws:states:us-east-1:123:execution:gc:x"
    }
    rt = runtime_mod.Runtime(
        config=config, registry=Registry(config, secrets), store=store, sfn=sfn, lam=MagicMock()
    )
    for mod in ("runtime", "router", "triage", "github_hook", "lifecycle"):
        monkeypatch.setattr(f"grumpycat.handlers.{mod}.runtime", lambda: rt)
    return rt


def http_event(
    path: str, body: dict[str, Any] | str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    raw = body if isinstance(body, str) else json.dumps(body)
    return {
        "rawPath": path,
        "routeKey": f"POST {path}",
        "headers": headers or {},
        "body": raw,
        "isBase64Encoded": False,
    }
