"""Module-level fake plugins so `EntryPoint.load()` can import them by dotted path."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from grumpycat.core.models import (
    Brief,
    CIFailure,
    EngineResult,
    ErrorEvent,
    Evidence,
    IssueState,
    WorkerTask,
)
from grumpycat.plugins.spec import (
    CIPlugin,
    EnginePlugin,
    InputPlugin,
    OutputPlugin,
    PluginKind,
    PluginSpec,
    Trigger,
)


class FakeInConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site: str = "example.test"


class FakeInput(InputPlugin):
    spec = PluginSpec(
        name="fake_in",
        kind=PluginKind.INPUT,
        config_schema=FakeInConfig,
        required_secrets=("FAKE_TOKEN",),
        optional_tools=("definitely-not-a-real-cli",),
        trigger=Trigger.HTTP,
    )

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        return headers.get("x-fake-sig") == "ok"

    def parse(self, payload: dict[str, Any]) -> ErrorEvent | None:
        return None

    def enrich(self, event: ErrorEvent) -> Evidence:
        return Evidence()


class FakeEventInput(InputPlugin):
    spec = PluginSpec(
        name="fake_events",
        kind=PluginKind.INPUT,
        trigger=Trigger.EVENTBRIDGE,
        event_pattern={"source": ["aws.ecs"], "detail-type": ["ECS Task State Change"]},
    )

    def parse(self, payload: dict[str, Any]) -> ErrorEvent | None:
        return None

    def enrich(self, event: ErrorEvent) -> Evidence:
        return Evidence()


class FakeEngine(EnginePlugin):
    spec = PluginSpec(name="fake_engine", kind=PluginKind.ENGINE)

    def run(self, task: WorkerTask, workdir: Path, brief_md: str) -> EngineResult:
        return EngineResult(changed=False, summary="noop")


class FakeOutput(OutputPlugin):
    spec = PluginSpec(name="fake_out", kind=PluginKind.OUTPUT)

    def on_transition(
        self, state: IssueState, previous: IssueState | None, brief: Brief | None
    ) -> IssueState:
        return state


class WrongKind(OutputPlugin):
    """Registered as an input but declares itself an output."""

    spec = PluginSpec(name="wrong_kind", kind=PluginKind.OUTPUT)

    def on_transition(
        self, state: IssueState, previous: IssueState | None, brief: Brief | None
    ) -> IssueState:
        return state


class OldApi(EnginePlugin):
    spec = PluginSpec(name="old_api", kind=PluginKind.ENGINE, api_version=0)

    def run(self, task: WorkerTask, workdir: Path, brief_md: str) -> EngineResult:
        return EngineResult(changed=False, summary="noop")


class NoSpec:
    pass


class FakeCI(CIPlugin):
    spec = PluginSpec(name="fake_ci", kind=PluginKind.CI, required_secrets=("CI_TOKEN",))

    def fetch_failure(self, repo: str, sha: str, context: str, target_url: str | None) -> CIFailure:
        return CIFailure(excerpt=f"{repo}@{sha} {context} failed")
