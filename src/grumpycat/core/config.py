"""`grumpycat.yaml` — the one document a client supplies (as a Terraform module input).

The file is deliberately opaque to Terraform; only GC validates it. Plugin-specific sections
(`inputs.<name>`, `outputs.<name>`) are validated by each plugin's own `ConfigSchema` at
registry load time, not here, so adding a plugin never changes this module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from grumpycat.core.models import RepoTarget


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PageWhen(_Model):
    """Severity policy: page when `env` matches and any listed condition holds."""

    env: str | list[str] = "prod"
    level_fatal: bool = True
    users_15m: int | None = 50
    event_count_15m: int | None = None


class Policy(_Model):
    page_when: PageWhen = Field(default_factory=PageWhen)
    confidence_min: float = Field(default=0.6, ge=0.0, le=1.0)
    max_attempts: int = Field(default=3, ge=0)
    prs_per_day: int = Field(default=10, ge=0)
    cooldown_hours: int = Field(default=72, ge=0, description="After a closed-unmerged PR")
    gated: bool = Field(default=True, description="Require a Slack approval before each fix")
    freeze: bool = Field(default=False, description="Triage only; open nothing")


class CIConfig(_Model):
    provider: str = Field(description="CI plugin name, e.g. 'buildkite' or 'github_actions'")
    options: dict[str, Any] = Field(default_factory=dict, description="That plugin's config")


class RepoConfig(_Model):
    engine: str
    model: str | None = None
    default_branch: str = "main"
    ci_pipeline: str | None = Field(
        default=None, description="Pipeline identifier in the CI provider, e.g. 'acme/api'"
    )
    prepare: str | None = None
    worker_image: str | None = None
    labels: list[str] = Field(default_factory=lambda: ["grumpycat"])
    services: list[str] = Field(
        default_factory=list,
        description="Service names (from alerts) that map to this repo. Empty = repo name",
    )

    def to_target(self, full_name: str) -> RepoTarget:
        return RepoTarget(
            full_name=full_name,
            engine=self.engine,
            model=self.model,
            default_branch=self.default_branch,
            ci_pipeline=self.ci_pipeline,
            prepare=self.prepare,
            worker_image=self.worker_image,
            labels=self.labels,
        )


class Config(_Model):
    client: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,30}$")
    inputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    engines: dict[str, dict[str, Any]] = Field(default_factory=dict)
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ci: CIConfig | None = Field(default=None, description="How to read the target repos' CI")
    repos: dict[str, RepoConfig] = Field(default_factory=dict)
    policy: Policy = Field(default_factory=Policy)
    reviewer_allowlist: list[str] = Field(
        default_factory=list,
        description="GitHub logins (bots and humans) whose PR comments the groomer acts on",
    )

    @field_validator("repos")
    @classmethod
    def _repo_names(cls, v: dict[str, RepoConfig]) -> dict[str, RepoConfig]:
        for name in v:
            if name.count("/") != 1:
                msg = f"repo key must be owner/name, got {name!r}"
                raise ValueError(msg)
        return v

    def resolve_repo(self, service: str | None) -> tuple[str, RepoConfig] | None:
        """Map an alert's service name to a repo. Exact service match wins, then repo name."""
        if service is None:
            return None
        for name, repo in self.repos.items():
            if service in repo.services:
                return name, repo
        for name, repo in self.repos.items():
            if not repo.services and name.split("/", 1)[1] == service:
                return name, repo
        return None


def load_config(source: str | Path | None = None) -> Config:
    """Load from a path, a YAML string, or `$GRUMPYCAT_CONFIG` (path or inline YAML).

    Lambdas and the worker receive the document through SSM → env var; tests pass a string.
    """
    if source is None:
        source = os.environ.get("GRUMPYCAT_CONFIG")
        if source is None:
            msg = "no config: pass a path/YAML string or set GRUMPYCAT_CONFIG"
            raise RuntimeError(msg)
    text = source.read_text() if isinstance(source, Path) else str(source)
    if "\n" not in text and Path(text).is_file():
        text = Path(text).read_text()
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        msg = "grumpycat.yaml must be a mapping at the top level"
        raise ValueError(msg)
    return Config.model_validate(raw)
