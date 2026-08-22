"""Sentry input: Internal Integration webhooks for issues, plus issue-alert event webhooks.

Setup (per Sentry organisation):
  Settings → Developer Settings → Internal Integrations → New
    Webhook URL: <module output>/in/sentry
    Permissions: Issue & Event: Read (Write to let Grumpycat comment/resolve)
    Webhooks: issue  (and optionally: error / event alerts)
  The integration's *client secret* signs webhooks → secret map key SENTRY_WEBHOOK_SECRET.
  The integration's *token* reads the API          → secret map key SENTRY_AUTH_TOKEN.

Resource types handled (``Sentry-Hook-Resource`` header):
  issue        action created → NEW, unresolved (substatus regressed) → REGRESSION,
               resolved → RECOVERED; everything else ignored.
  event_alert  an alert rule firing with "send a notification via <integration>" → NEW
               (the payload carries the full event, so `enrich` is cheap).

Fingerprint: ``sentry:<org>:<issue id>`` — Sentry's own grouping is the dedupe key.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from grumpycat.core.models import (
    ErrorEvent,
    Evidence,
    Exception_,
    IssueState,
    StackFrame,
    Transition,
)
from grumpycat.core.scrub import scrub_mapping, scrub_text
from grumpycat.plugins.spec import InputPlugin, PluginKind, PluginSpec, Trigger

SIGNATURE_HEADER = "sentry-hook-signature"
RESOURCE_HEADER = "sentry-hook-resource"


class SentryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str = Field(description="Organisation slug, e.g. 'acme'")
    url: HttpUrl = Field(
        default=HttpUrl("https://sentry.io"), description="Base URL; change for self-hosted"
    )
    projects: list[str] = Field(
        default_factory=list, description="Project slugs to accept; empty = all"
    )
    environments: list[str] = Field(
        default_factory=list, description="Environments to accept; empty = all"
    )
    min_level: str = Field(default="error", description="Ignore issues below this level")


_LEVELS = ["debug", "info", "warning", "error", "fatal"]


def _level_ok(level: str | None, minimum: str) -> bool:
    if level is None:
        return True
    try:
        return _LEVELS.index(level) >= _LEVELS.index(minimum)
    except ValueError:
        return True


def _ts(value: str | float | int | None) -> datetime:
    if value is None:
        return datetime.now(tz=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _looks_like_sha(value: str | None) -> str | None:
    if value and len(value) >= 7 and all(c in "0123456789abcdef" for c in value.lower()):
        return value
    return None


class SentryInput(InputPlugin):
    spec = PluginSpec(
        name="sentry",
        kind=PluginKind.INPUT,
        config_schema=SentryConfig,
        required_secrets=("SENTRY_WEBHOOK_SECRET", "SENTRY_AUTH_TOKEN"),
        optional_tools=("sentry-cli",),
        trigger=Trigger.HTTP,
    )
    config: SentryConfig

    # -- verification ---------------------------------------------------------------------

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        given = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER)
        if not given:
            return False
        expected = hmac.new(
            self.secrets["SENTRY_WEBHOOK_SECRET"].encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, given)

    # -- parsing --------------------------------------------------------------------------

    def parse(self, payload: dict[str, Any]) -> ErrorEvent | None:
        """Route on the resource type, which the router copies into ``payload["_resource"]``
        from the ``Sentry-Hook-Resource`` header (Sentry puts it in a header, not the body)."""
        resource = payload.get("_resource") or _guess_resource(payload)
        if resource == "issue":
            return self._parse_issue(payload)
        if resource in {"event_alert", "error"}:
            return self._parse_event(payload)
        return None

    def _accept(self, project: str | None, environment: str | None, level: str | None) -> bool:
        if self.config.projects and project not in self.config.projects:
            return False
        if self.config.environments and environment not in self.config.environments:
            return False
        return _level_ok(level, self.config.min_level)

    def _parse_issue(self, payload: dict[str, Any]) -> ErrorEvent | None:
        action = payload.get("action")
        issue = (payload.get("data") or {}).get("issue") or {}
        if not issue.get("id"):
            return None
        substatus = issue.get("substatus")
        if action == "created":
            transition = Transition.NEW
        elif action == "unresolved" and substatus in {"regressed", None}:
            transition = Transition.REGRESSION
        elif action == "resolved":
            transition = Transition.RECOVERED
        else:
            return None
        project = (issue.get("project") or {}).get("slug")
        if not self._accept(project, None, issue.get("level")):
            return None
        return ErrorEvent(
            source="sentry",
            fingerprint=f"sentry:{self.config.org}:{issue['id']}",
            transition=transition,
            service=project,
            env=None,  # issue webhooks don't carry environment; enrich() fills it from the event
            title=scrub_text(issue.get("title")) or "Sentry issue",
            url=issue.get("permalink"),
            occurred_at=_ts(issue.get("lastSeen") or issue.get("firstSeen")),
            source_ids={"issue_id": str(issue["id"]), "short_id": str(issue.get("shortId", ""))},
            tags={"level": str(issue.get("level", ""))},
        )

    def _parse_event(self, payload: dict[str, Any]) -> ErrorEvent | None:
        data = payload.get("data") or {}
        event = data.get("event") or data.get("error") or {}
        issue_id = event.get("issue_id") or event.get("groupID")
        if not issue_id:
            return None
        tags = (
            {k: str(v) for k, v in (event.get("tags") or [])}
            if isinstance(event.get("tags"), list)
            else {}
        )
        project = event.get("project_slug") or tags.get("project")
        environment = event.get("environment") or tags.get("environment")
        if not self._accept(project, environment, event.get("level")):
            return None
        return ErrorEvent(
            source="sentry",
            fingerprint=f"sentry:{self.config.org}:{issue_id}",
            transition=Transition.NEW,
            service=project,
            env=environment,
            title=scrub_text(event.get("title")) or "Sentry event",
            url=event.get("web_url") or event.get("issue_url"),
            occurred_at=_ts(event.get("datetime") or event.get("timestamp")),
            source_ids={
                "issue_id": str(issue_id),
                "event_id": str(event.get("event_id", "")),
                "rule": str(data.get("triggered_rule") or ""),
            },
            tags={
                "level": str(event.get("level", "")),
                **{k: v for k, v in tags.items() if k in {"release", "server_name", "transaction"}},
            },
        )

    # -- enrichment -----------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"{str(self.config.url).rstrip('/')}/api/0",
            headers={"Authorization": f"Bearer {self.secrets['SENTRY_AUTH_TOKEN']}"},
            timeout=20.0,
            transport=self.transport,
        )

    def enrich(self, event: ErrorEvent) -> Evidence:
        issue_id = event.source_ids["issue_id"]
        with self._client() as c:
            issue = c.get(f"/issues/{issue_id}/").raise_for_status().json()
            latest = c.get(f"/issues/{issue_id}/events/latest/").raise_for_status().json()
        return evidence_from_event(issue, latest)

    def annotate(self, event: ErrorEvent, state: IssueState, text: str) -> None:
        """Leave a note on the issue (needs Issue & Event: Write)."""
        with self._client() as c:
            c.post(
                f"/issues/{event.source_ids['issue_id']}/notes/", json={"text": text}
            ).raise_for_status()


def _guess_resource(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") or {}
    if "issue" in data:
        return "issue"
    if "event" in data or "error" in data:
        return "event_alert"
    return None


def evidence_from_event(issue: dict[str, Any], event: dict[str, Any]) -> Evidence:
    """Pure transformer from Sentry's issue + event JSON into `Evidence` (scrubbed)."""
    exception = None
    request = None
    for entry in event.get("entries") or []:
        if entry.get("type") == "exception":
            values = (entry.get("data") or {}).get("values") or []
            if values:
                v = values[-1]  # innermost / most recent
                frames = [
                    StackFrame(
                        filename=f.get("filename") or f.get("absPath") or "?",
                        lineno=f.get("lineNo"),
                        function=f.get("function"),
                        in_app=bool(f.get("inApp", True)),
                    )
                    for f in ((v.get("stacktrace") or {}).get("frames") or [])
                ]
                exception = Exception_(
                    type=str(v.get("type") or "Error"),
                    value=scrub_text(str(v.get("value") or "")) or "",
                    frames=frames,
                )
        elif entry.get("type") == "request":
            d = entry.get("data") or {}
            request = scrub_mapping(
                {
                    "method": d.get("method"),
                    "url": d.get("url"),
                    "query": d.get("query"),
                    "headers": dict(d.get("headers") or []),
                }
            )
    tags = {t.get("key"): t.get("value") for t in (event.get("tags") or []) if t.get("key")}
    release = (event.get("release") or {}).get("version") or tags.get("release")
    links: dict[str, HttpUrl] = {}
    if issue.get("permalink"):
        links["sentry"] = HttpUrl(issue["permalink"])
    hints = []
    if event.get("culprit") or issue.get("culprit"):
        hints.append(f"culprit: {event.get('culprit') or issue.get('culprit')}")
    if tags.get("transaction"):
        hints.append(f"transaction: {tags['transaction']}")
    return Evidence(
        exception=exception,
        signature=f"{exception.type}: {exception.value[:120]}" if exception else None,
        message=scrub_text(event.get("message") or issue.get("title")),
        first_seen=_ts(issue["firstSeen"]) if issue.get("firstSeen") else None,
        last_seen=_ts(issue["lastSeen"]) if issue.get("lastSeen") else None,
        event_count=int(issue["count"]) if str(issue.get("count", "")).isdigit() else None,
        user_count=issue.get("userCount"),
        deployed_sha=_looks_like_sha(release),
        request=request,
        links=links,
        routing_hints=hints,
    )
