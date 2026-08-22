"""GitHub Actions CI plugin: from a failed check run, fetch the failing job's log tail.

The `check_run` webhook carries `details_url` like
`https://github.com/<owner>/<repo>/actions/runs/<run_id>/job/<job_id>`; a commit `status`
from Actions carries the run URL. Uses the same GitHub App credentials as the output.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from grumpycat.core import github_auth
from grumpycat.core.models import CIFailure
from grumpycat.plugins.spec import CIPlugin, PluginKind, PluginSpec

_JOB_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)(?:/jobs?/(\d+))?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z ", re.M)


class GitHubActionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_url: HttpUrl = Field(default=HttpUrl("https://api.github.com"))
    tail_bytes: int = Field(default=24_000, ge=1_000, le=200_000)


class GitHubActionsCI(CIPlugin):
    spec = PluginSpec(
        name="github_actions",
        kind=PluginKind.CI,
        config_schema=GitHubActionsConfig,
        required_secrets=github_auth.REQUIRED,
    )
    config: GitHubActionsConfig
    transport: Any = None

    def _client(self) -> httpx.Client:
        base = str(self.config.api_url).rstrip("/")
        token = github_auth.installation_token(self.secrets, api_url=base, transport=self.transport)
        return httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
            transport=self.transport,
            follow_redirects=True,
        )

    def fetch_failure(self, repo: str, sha: str, context: str, target_url: str | None) -> CIFailure:
        m = _JOB_URL.search(target_url or "")
        if not m:
            return CIFailure(
                excerpt=f"{context} failed; no Actions run URL on the status ({target_url})",
                build_url=HttpUrl(target_url) if target_url else None,
            )
        owner, name, run_id, job_id = m.group(1), m.group(2), m.group(3), m.group(4)
        with self._client() as c:
            if job_id is None:
                jobs = (
                    c.get(f"/repos/{owner}/{name}/actions/runs/{run_id}/jobs")
                    .raise_for_status()
                    .json()
                )
                failed = [
                    j
                    for j in jobs.get("jobs", [])
                    if j.get("conclusion") in {"failure", "timed_out", "cancelled"}
                ]
                if not failed:
                    return CIFailure(
                        excerpt=f"run {run_id} has no failed jobs",
                        build_url=HttpUrl(target_url or ""),
                    )
                job = failed[0]
                job_id = str(job["id"])
                job_name = str(job.get("name") or "job")
                job_url = str(job.get("html_url") or target_url)
                extra = f" (+{len(failed) - 1} more failed jobs)" if len(failed) > 1 else ""
            else:
                job = (
                    c.get(f"/repos/{owner}/{name}/actions/jobs/{job_id}").raise_for_status().json()
                )
                job_name = str(job.get("name") or "job")
                job_url = str(job.get("html_url") or target_url)
                extra = ""
            log = c.get(f"/repos/{owner}/{name}/actions/jobs/{job_id}/logs").raise_for_status().text
        content = _TS.sub("", _ANSI.sub("", log))
        raw = content.encode()
        truncated = len(raw) > self.config.tail_bytes
        excerpt = raw[-self.config.tail_bytes :].decode("utf-8", "ignore") if truncated else content
        return CIFailure(
            build_url=HttpUrl(job_url),
            job_name=job_name + extra,
            excerpt=excerpt.strip(),
            truncated=truncated,
        )
