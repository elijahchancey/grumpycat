"""Domain models shared by every plugin and every runtime (Lambda, worker, state machine).

Everything that crosses a process boundary is a pydantic model so it can be serialised
into Step Functions input, DynamoDB items, and SQS/EventBridge payloads without ad-hoc dicts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Transition(StrEnum):
    """What the source is telling us about the error's lifecycle."""

    NEW = "new"  # first time the source has seen this signature
    REGRESSION = "regression"  # previously resolved, now back
    RECOVERED = "recovered"  # source says it cleared; annotate, never open
    UPDATE = "update"  # more events on an already-known signature


class ErrorEvent(_Model):
    """A normalised error as parsed from an input plugin's payload, before enrichment.

    `fingerprint` is the dedupe key: one open issue (and one PR) per fingerprint. Inputs are
    responsible for choosing something stable across re-fires of the same underlying defect
    (e.g. Sentry issue id; Datadog `monitor_id + group + dominant error signature`).
    """

    source: str = Field(description="Input plugin name, e.g. 'sentry'")
    fingerprint: str
    transition: Transition
    service: str | None = None
    env: str | None = None
    title: str
    url: HttpUrl | None = Field(default=None, description="Deep link to the source issue/alert")
    occurred_at: datetime
    source_ids: dict[str, str] = Field(
        default_factory=dict,
        description="Opaque ids the input needs later (issue id, monitor id, event id, ...)",
    )
    tags: dict[str, str] = Field(default_factory=dict)


class StackFrame(_Model):
    filename: str
    lineno: int | None = None
    function: str | None = None
    in_app: bool = True


class Exception_(_Model):  # noqa: N801 - avoids shadowing the builtin
    type: str
    value: str
    frames: list[StackFrame] = Field(default_factory=list, description="Innermost last")


class Evidence(_Model):
    """What the input plugin could gather about the error, for the brief and for triage.

    Deliberately small: the engine can pull more itself through the repo's own skills/MCP.
    Inputs must scrub PII before populating anything here.
    """

    exception: Exception_ | None = None
    signature: str | None = Field(
        default=None,
        description=(
            "Stable error signature discovered during enrichment (e.g. dominant exception "
            "type+message). Triage folds it into the final fingerprint for sources whose "
            "payload alone can't identify the defect (Datadog monitors, ECS exits)."
        ),
    )
    message: str | None = Field(default=None, description="Source's own message body, verbatim")
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    event_count: int | None = None
    user_count: int | None = None
    deployed_sha: str | None = Field(default=None, description="Release/commit the error ran on")
    request: dict[str, Any] | None = Field(default=None, description="Scrubbed request shape")
    sample_logs: list[str] = Field(default_factory=list)
    links: dict[str, HttpUrl] = Field(default_factory=dict)
    routing_hints: list[str] = Field(
        default_factory=list,
        description="Free-text pointers for the engine, e.g. an endpoint or task family name",
    )


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Triage(_Model):
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0, description="Belief that this is a code defect")
    page: bool = Field(description="Policy says wake someone up")
    rationale: str


class EngineKind(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class RepoTarget(_Model):
    """Where the fix goes and how. Resolved from `grumpycat.yaml` by service name."""

    full_name: str = Field(description="owner/name")
    engine: str
    model: str | None = None
    default_branch: str = "main"
    ci_pipeline: str | None = Field(default=None, description="Pipeline id in the CI provider")
    prepare: str | None = Field(default=None, description="Shell run before the engine")
    worker_image: str | None = Field(default=None, description="Escape hatch: per-repo image")
    labels: list[str] = Field(default_factory=lambda: ["grumpycat"])


class Brief(_Model):
    """Everything the worker hands to an engine. Rendered to markdown by `core.brief`."""

    event: ErrorEvent
    evidence: Evidence
    triage: Triage
    target: RepoTarget
    previous_pr_url: HttpUrl | None = Field(
        default=None, description="For regressions: the PR that fixed it last time"
    )


class TaskKind(StrEnum):
    FIX = "fix"
    SHEPHERD = "shepherd"


class ShepherdTrigger(_Model):
    """Why a shepherd run was started. Exactly one of the optional fields is set."""

    ci_failure_log: str | None = None
    review_findings: list[str] = Field(default_factory=list)
    actor: str | None = None


class WorkerTask(_Model):
    """Step Functions -> Fargate task input."""

    kind: TaskKind
    brief: Brief
    branch: str
    pr_number: int | None = None
    attempt: int = 1
    trigger: ShepherdTrigger | None = None
    task_token: str | None = Field(default=None, description="Step Functions callback token")


class EngineResult(_Model):
    """What an engine run produced. `changed` False means it declined (see `report`)."""

    changed: bool
    summary: str
    report: str | None = Field(default=None, description="Engine's REPORT.md when it declines")
    cost_usd: float | None = None
    turns: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict, description="Engine-specific metadata")


class CIFailure(_Model):
    """What a CI plugin could recover about a failed build, for the shepherd prompt."""

    build_url: HttpUrl | None = None
    job_name: str | None = None
    excerpt: str = Field(description="Tail of the failing job's log, already size-capped")
    truncated: bool = False


class IssueStatus(StrEnum):
    TRIAGED = "triaged"
    AWAITING_APPROVAL = "awaiting_approval"
    FIXING = "fixing"
    PR_OPEN = "pr_open"
    SHEPHERDING = "shepherding"
    READY = "ready"
    NEEDS_HUMAN = "needs_human"
    RCA_ONLY = "rca_only"
    MERGED = "merged"
    CLOSED = "closed"


class IssueState(_Model):
    """DynamoDB item, keyed by fingerprint. The single source of truth for one error."""

    fingerprint: str
    status: IssueStatus
    event: ErrorEvent
    triage: Triage | None = None
    target: RepoTarget | None = None
    pr_number: int | None = None
    pr_url: HttpUrl | None = None
    branch: str | None = None
    execution_arn: str | None = None
    attempts: int = 0
    cost_usd: float = 0.0
    slack_thread_ts: str | None = None
    created_at: datetime
    updated_at: datetime
