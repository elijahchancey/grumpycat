"""Buildkite CI plugin: turn a failed commit status into the failing job's log tail.

The GitHub `status` event's `target_url` is `https://buildkite.com/<org>/<pipeline>/builds/<n>`;
that is all we need. Secrets: BUILDKITE_API_TOKEN (read_builds + read_build_logs).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from grumpycat.core.models import CIFailure
from grumpycat.plugins.spec import CIPlugin, PluginKind, PluginSpec

_BUILD_URL = re.compile(r"buildkite\.com/([^/]+)/([^/]+)/builds/(\d+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b_bk;t=\d+\x07")
FAILED_STATES = {"failed", "timed_out", "broken", "canceled"}


class BuildkiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str | None = Field(default=None, description="Default org slug if the URL lacks one")
    api_url: str = "https://api.buildkite.com/v2"
    tail_bytes: int = Field(default=24_000, ge=1_000, le=200_000)


class BuildkiteCI(CIPlugin):
    spec = PluginSpec(
        name="buildkite",
        kind=PluginKind.CI,
        config_schema=BuildkiteConfig,
        required_secrets=("BUILDKITE_API_TOKEN",),
    )
    config: BuildkiteConfig
    transport: Any = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.config.api_url,
            headers={"Authorization": f"Bearer {self.secrets['BUILDKITE_API_TOKEN']}"},
            timeout=30.0,
            transport=self.transport,
        )

    def fetch_failure(self, repo: str, sha: str, context: str, target_url: str | None) -> CIFailure:
        m = _BUILD_URL.search(target_url or "")
        if not m:
            return CIFailure(
                excerpt=f"{context} failed; no Buildkite build URL on the status ({target_url})",
                build_url=HttpUrl(target_url) if target_url else None,
            )
        org, pipeline, number = m.group(1), m.group(2), m.group(3)
        with self._client() as c:
            build = (
                c.get(f"/organizations/{org}/pipelines/{pipeline}/builds/{number}")
                .raise_for_status()
                .json()
            )
            jobs = [
                j
                for j in build.get("jobs", [])
                if j.get("state") in FAILED_STATES and j.get("type") == "script"
            ]
            if not jobs:
                return CIFailure(
                    excerpt=f"build #{number} is {build.get('state')}; no failed script jobs found",
                    build_url=HttpUrl(build.get("web_url") or target_url or ""),
                )
            job = jobs[0]
            log = (
                c.get(
                    f"/organizations/{org}/pipelines/{pipeline}/builds/{number}/jobs/{job['id']}/log"
                )
                .raise_for_status()
                .json()
            )
        content = _ANSI.sub("", str(log.get("content") or ""))
        truncated = len(content.encode()) > self.config.tail_bytes
        excerpt = (
            content.encode()[-self.config.tail_bytes :].decode("utf-8", "ignore")
            if truncated
            else content
        )
        name = str(job.get("name") or job.get("label") or job.get("command") or "job")
        extra = f" (+{len(jobs) - 1} more failed jobs)" if len(jobs) > 1 else ""
        return CIFailure(
            build_url=HttpUrl(job.get("web_url") or build.get("web_url") or target_url or ""),
            job_name=name + extra,
            excerpt=excerpt.strip(),
            truncated=truncated,
        )
