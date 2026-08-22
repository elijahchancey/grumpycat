"""The plugin contract. Read `docs/plugins.md` before writing one.

A plugin is a class with a `spec: PluginSpec` class attribute and a constructor that takes its
validated config. It is discovered through an `importlib.metadata` entry point in one of the
groups `grumpycat.inputs`, `grumpycat.engines`, `grumpycat.outputs`.

Plugins never read environment variables for credentials directly; they declare
`required_secrets`, and the registry hands them a `Secrets` mapping at construction. That is
what lets the worker and the Lambdas fail fast at startup instead of mid-run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from grumpycat import PLUGIN_API_VERSION
from grumpycat.core.models import (
    Brief,
    EngineResult,
    ErrorEvent,
    Evidence,
    IssueState,
    WorkerTask,
)

Secrets = Mapping[str, str]


class PluginKind(StrEnum):
    INPUT = "input"
    ENGINE = "engine"
    OUTPUT = "output"


class Trigger(StrEnum):
    """How an input receives events."""

    HTTP = "http"  # POST /in/<name>; the plugin verifies a signature
    EVENTBRIDGE = "eventbridge"  # a rule with `event_pattern` targets the router Lambda


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PluginSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    kind: PluginKind
    api_version: int = PLUGIN_API_VERSION
    config_schema: type[BaseModel] = EmptyConfig
    required_secrets: tuple[str, ...] = Field(
        default=(), description="Env-var names the registry must be able to supply"
    )
    optional_tools: tuple[str, ...] = Field(
        default=(), description="CLIs the plugin benefits from; missing ones only warn"
    )
    # inputs only
    trigger: Trigger | None = None
    event_pattern: dict[str, Any] | None = Field(
        default=None, description="EventBridge pattern; Terraform creates the rule from it"
    )


class Plugin(ABC):
    spec: ClassVar[PluginSpec]

    def __init__(self, config: BaseModel, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets


class InputPlugin(Plugin):
    """Turns a source payload into an `ErrorEvent`, then gathers `Evidence` for it."""

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        """HTTP inputs must override. EventBridge inputs are trusted by the rule itself."""
        return self.spec.trigger is Trigger.EVENTBRIDGE

    @abstractmethod
    def parse(self, payload: dict[str, Any]) -> ErrorEvent | None:
        """Return None for payloads that are not errors (pings, test events, recoveries we
        don't track). Must not make network calls."""

    @abstractmethod
    def enrich(self, event: ErrorEvent) -> Evidence:
        """Fetch brief-level evidence from the source API. Must scrub PII. Network allowed."""

    def annotate(self, event: ErrorEvent, state: IssueState, text: str) -> None:
        """Write back to the source (e.g. comment the PR link on the issue). Optional."""
        return None


class EnginePlugin(Plugin):
    """Runs a coding agent inside a checkout. Never runs the test suite; CI does."""

    @abstractmethod
    def run(self, task: WorkerTask, workdir: Path, brief_md: str) -> EngineResult:
        """Edit files in `workdir`. Do not commit; the worker commits and pushes."""

    def preflight(self, workdir: Path) -> list[str]:
        """Return human-readable problems (missing CLI, bad auth). Empty = OK."""
        return []


class OutputPlugin(Plugin):
    """Reacts to issue-state transitions: opens PRs, posts to Slack, etc."""

    @abstractmethod
    def on_transition(
        self, state: IssueState, previous: IssueState | None, brief: Brief | None
    ) -> IssueState:
        """May return an updated state (e.g. with `pr_number` filled in)."""
