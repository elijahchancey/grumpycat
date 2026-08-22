"""Datadog input: monitor notifications via the Webhooks integration.

Setup (per Datadog organisation):
  Integrations → Webhooks → New
    Name:    grumpycat
    URL:     <module output>/in/datadog
    Payload: the JSON in `WEBHOOK_PAYLOAD_TEMPLATE` below (copy it verbatim)
    Custom headers: {"X-Grumpycat-Token": "<value of DD_WEBHOOK_TOKEN>"}
  Then add `@webhook-grumpycat` to the message of every monitor Grumpycat should see.

A monitor notification tells us *that* something is wrong, rarely *what*. The payload never
carries a stack trace, so `enrich` reads the monitor definition and then searches the
matching telemetry for the alert window:

  monitor type                 search                         signature
  ---------------------------  -----------------------------  ----------------------------
  log alert                    Logs search, monitor's query   @error.kind + message prefix
  metric / query / trace       Spans search, @error:true      @error.type + message prefix
  error-tracking / rum         RUM or Spans with @type:error  same

The dominant error signature (≥ `dominant_share` of matches) becomes `Evidence.signature`;
triage folds it into the fingerprint so one endpoint breaking two different ways is two
issues, and the same breakage re-firing is one. No dominant signature → no PR (RCA only).

Error Tracking *issues* (APM/RUM/Logs) arrive through the same path when alerted by an
Error Tracking monitor; the issue id, when the notification link carries it, is used to
narrow the search.
"""

from __future__ import annotations

import hmac
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from grumpycat.core.models import ErrorEvent, Evidence, Exception_, IssueState, Transition
from grumpycat.core.scrub import scrub_text
from grumpycat.plugins.spec import InputPlugin, PluginKind, PluginSpec, Trigger

TOKEN_HEADER = "x-grumpycat-token"  # noqa: S105 - a header name, not a credential

WEBHOOK_PAYLOAD_TEMPLATE = """{
  "alert_id": "$ALERT_ID",
  "event_id": "$EVENT_ID",
  "transition": "$ALERT_TRANSITION",
  "alert_type": "$ALERT_TYPE",
  "title": "$EVENT_TITLE",
  "scope": "$ALERT_SCOPE",
  "query": "$ALERT_QUERY",
  "tags": "$TAGS",
  "priority": "$PRIORITY",
  "date": "$DATE",
  "last_updated": "$LAST_UPDATED",
  "link": "$LINK",
  "message": "$TEXT_ONLY_MSG",
  "org": {"id": "$ORG_ID", "name": "$ORG_NAME"}
}"""

_TRANSITIONS: dict[str, Transition | None] = {
    "Triggered": Transition.NEW,
    "Re-Triggered": Transition.UPDATE,
    "Renotify": Transition.UPDATE,
    "Recovered": Transition.RECOVERED,
    "Warn": None,
    "No Data": None,
    "Warn Recovered": None,
}

MonitorKind = Literal["log", "spans", "rum", "unknown"]


class DatadogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: str = Field(default="datadoghq.com", description="e.g. datadoghq.com, datadoghq.eu")
    lookback_minutes: int = Field(default=15, ge=1, le=240)
    max_events: int = Field(default=100, ge=10, le=1000)
    dominant_share: float = Field(default=0.5, ge=0.0, le=1.0)
    monitors: list[int] = Field(
        default_factory=list, description="Monitor ids to accept; empty = all"
    )
    envs: list[str] = Field(default_factory=list, description="env values to accept; empty = all")
    token_header: str = Field(default="X-Grumpycat-Token")


def parse_scope(scope: str | None) -> dict[str, str]:
    """'resource_name:delete_/v1/x,env:prod' → {'resource_name': 'delete_/v1/x', 'env': 'prod'}."""
    out: dict[str, str] = {}
    for part in (scope or "").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def parse_tags(tags: str | None) -> dict[str, str]:
    return parse_scope(tags)


_METRIC_TAGS = re.compile(r"\{([^}]*)\}")
_LOGS_QUERY = re.compile(r'logs\("((?:[^"\\]|\\.)*)"\)')
_TRACE_QUERY = re.compile(r'trace-analytics\("((?:[^"\\]|\\.)*)"\)')
_RUM_QUERY = re.compile(r'rum\("((?:[^"\\]|\\.)*)"\)')


def filters_from_metric_query(query: str) -> dict[str, str]:
    """Positive tag filters from the first {…} block of a metric query (service, env, ...)."""
    m = _METRIC_TAGS.search(query or "")
    if not m:
        return {}
    out: dict[str, str] = {}
    for raw in m.group(1).split(","):
        raw = raw.strip()
        if not raw or raw.startswith("!") or ":" not in raw:
            continue
        k, v = raw.split(":", 1)
        if k not in out:  # first wins; later ones are usually IN (...) expansions
            out[k] = v
    return out


def _ms(value: str | int | float | None) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n > 1e12:  # milliseconds
        n /= 1000
    return datetime.fromtimestamp(n, tz=UTC)


def _issue_id_from_link(link: str | None) -> str | None:
    if not link:
        return None
    qs = parse_qs(urlparse(link).query)
    for key in ("issueId", "issue_id", "error_tracking_issue_id"):
        if qs.get(key):
            return qs[key][0]
    return None


class DatadogInput(InputPlugin):
    spec = PluginSpec(
        name="datadog",
        kind=PluginKind.INPUT,
        config_schema=DatadogConfig,
        required_secrets=("DD_WEBHOOK_TOKEN", "DD_API_KEY", "DD_APP_KEY"),
        optional_tools=("datadog-ci",),
        trigger=Trigger.HTTP,
    )
    config: DatadogConfig

    # -- verification ---------------------------------------------------------------------

    def verify(self, headers: Mapping[str, str], body: bytes) -> bool:
        given = {k.lower(): v for k, v in headers.items()}.get(self.config.token_header.lower())
        if not given:
            return False
        return hmac.compare_digest(self.secrets["DD_WEBHOOK_TOKEN"], given)

    # -- parsing --------------------------------------------------------------------------

    def parse(self, payload: dict[str, Any]) -> ErrorEvent | None:
        alert_id = str(payload.get("alert_id") or "")
        if not alert_id.isdigit():
            return None
        if self.config.monitors and int(alert_id) not in self.config.monitors:
            return None
        transition = _TRANSITIONS.get(str(payload.get("transition") or ""))
        if transition is None:
            return None
        scope = parse_scope(payload.get("scope"))
        tags = parse_tags(payload.get("tags"))
        env = scope.get("env") or tags.get("env")
        if self.config.envs and env not in self.config.envs:
            return None
        service = scope.get("service") or tags.get("service")
        scope_key = payload.get("scope") or "*"
        return ErrorEvent(
            source="datadog",
            fingerprint=f"datadog:{alert_id}:{scope_key}",
            transition=transition,
            service=service,
            env=env,
            title=scrub_text(str(payload.get("title") or "Datadog monitor")) or "Datadog monitor",
            url=payload.get("link") or None,
            occurred_at=_ms(payload.get("last_updated"))
            or _ms(payload.get("date"))
            or datetime.now(tz=UTC),
            source_ids={
                "monitor_id": alert_id,
                "event_id": str(payload.get("event_id") or ""),
                "scope": str(payload.get("scope") or ""),
                "issue_id": _issue_id_from_link(payload.get("link")) or "",
                "message": str(payload.get("message") or "")[:4000],
                "query": str(payload.get("query") or "")[:2000],
            },
            tags={
                k: v
                for k, v in {**tags, **scope}.items()
                if k in {"env", "service", "cluster", "resource_name", "team"}
            },
        )

    # -- enrichment -----------------------------------------------------------------------

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"https://api.{self.config.site}",
            headers={
                "DD-API-KEY": self.secrets["DD_API_KEY"],
                "DD-APPLICATION-KEY": self.secrets["DD_APP_KEY"],
                "Content-Type": "application/json",
            },
            timeout=30.0,
            transport=self.transport,
        )

    def enrich(self, event: ErrorEvent) -> Evidence:
        monitor_id = event.source_ids["monitor_id"]
        scope = parse_scope(event.source_ids.get("scope"))
        with self._client() as c:
            monitor = c.get(f"/api/v1/monitor/{monitor_id}").raise_for_status().json()
            kind, query = classify_monitor(monitor, scope)
            to = event.occurred_at
            frm = to - timedelta(minutes=self.config.lookback_minutes)
            issue_id = event.source_ids.get("issue_id") or None
            if kind == "log":
                hits = self._search(c, "/api/v2/logs/events/search", query, frm, to)
                sig, sample = dominant_signature(hits, "log", self.config.dominant_share)
            elif kind == "spans":
                if issue_id:
                    query = f"{query} @issue.id:{issue_id}".strip()
                hits = self._search(c, "/api/v2/spans/events/search", query, frm, to)
                sig, sample = dominant_signature(hits, "spans", self.config.dominant_share)
            elif kind == "rum":
                if issue_id:
                    query = f"{query} @issue.id:{issue_id}".strip()
                hits = self._search(c, "/api/v2/rum/events/search", query, frm, to)
                sig, sample = dominant_signature(hits, "rum", self.config.dominant_share)
            else:
                hits, sig, sample = [], None, None
        return build_evidence(
            event, monitor, kind, query, hits, sig, sample, frm, to, self.config.site
        )

    def _search(
        self, c: httpx.Client, path: str, query: str, frm: datetime, to: datetime
    ) -> list[dict[str, Any]]:
        body = {
            "filter": {"query": query, "from": frm.isoformat(), "to": to.isoformat()},
            "page": {"limit": self.config.max_events},
            "sort": "-timestamp",
        }
        r = c.post(path, json=body).raise_for_status().json()
        data = r.get("data") or []
        return [d for d in data if isinstance(d, dict)]

    def annotate(self, event: ErrorEvent, state: IssueState, text: str) -> None:
        """Post an event on the monitor's timeline (shows in the monitor's event stream)."""
        with self._client() as c:
            c.post(
                "/api/v1/events",
                json={
                    "title": f"grumpycat: {state.status}",
                    "text": text,
                    "tags": [f"monitor_id:{event.source_ids['monitor_id']}", "source:grumpycat"],
                    "alert_type": "info",
                },
            ).raise_for_status()


# -- pure helpers (unit-tested without network) -------------------------------------------


def _inner(pattern: re.Pattern[str], query: str) -> str | None:
    m = pattern.search(query)
    return m.group(1).replace('\\"', '"') if m else None


def classify_monitor(monitor: dict[str, Any], scope: dict[str, str]) -> tuple[MonitorKind, str]:
    """Decide which telemetry to search and build the search query from the monitor + scope."""
    mtype = str(monitor.get("type") or "")
    mquery = str(monitor.get("query") or "")
    scope_terms = " ".join(f'{k}:"{v}"' if " " in v else f"{k}:{v}" for k, v in scope.items())

    def with_scope(*parts: str) -> str:
        return " ".join(p for p in (*parts, scope_terms) if p).strip()

    logs_q = _inner(_LOGS_QUERY, mquery)
    if mtype == "log alert" or logs_q is not None:
        return "log", with_scope(logs_q or "")

    rum_q = _inner(_RUM_QUERY, mquery)
    if mtype == "rum alert" or rum_q is not None:
        return "rum", with_scope(rum_q or "", "@type:error")

    if mtype == "error-tracking alert":
        # Error Tracking monitors can be APM, RUM or Logs sourced; the query names the source.
        low = mquery.lower()
        if "rum" in low:
            return "rum", with_scope("@type:error")
        if "log" in low:
            return "log", with_scope("status:error")
        return "spans", with_scope("@error:true")

    trace_q = _inner(_TRACE_QUERY, mquery)
    if mtype == "trace-analytics alert" or trace_q is not None:
        q = trace_q or ""
        return "spans", with_scope(q, "" if "@error" in q else "@error:true")

    if mtype in {"metric alert", "query alert"}:
        if "trace." not in mquery and not scope.get("resource_name"):
            return "unknown", ""
        filters = filters_from_metric_query(mquery)
        terms = " ".join(f"{k}:{v}" for k, v in filters.items() if k in {"service", "env"})
        return "spans", with_scope(terms, "@error:true")

    return "unknown", ""


def _attr(hit: dict[str, Any], *path: str) -> Any:
    cur: Any = hit.get("attributes") or {}
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _error_fields(
    hit: dict[str, Any], kind: MonitorKind
) -> tuple[str | None, str | None, str | None]:
    """(type, message, stack) for one search hit, across the three event shapes."""
    a = hit.get("attributes") or {}
    attrs = a.get("attributes") if isinstance(a.get("attributes"), dict) else a
    err = attrs.get("error") if isinstance(attrs, dict) else None
    if isinstance(err, dict):
        return (
            err.get("type") or err.get("kind"),
            err.get("message") or err.get("msg"),
            err.get("stack"),
        )
    if kind == "log":
        return (attrs.get("status") if isinstance(attrs, dict) else None, a.get("message"), None)
    return (None, a.get("message"), None)


def _normalise(msg: str | None) -> str:
    if not msg:
        return ""
    s = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b|[0-9a-f]{8}-[0-9a-f-]{27,}", "#", msg)
    return s.strip()[:120]


def dominant_signature(
    hits: list[dict[str, Any]], kind: MonitorKind, share: float
) -> tuple[str | None, dict[str, Any] | None]:
    """Cluster hits by (error type, normalised message); return the winner if it dominates."""
    if not hits:
        return None, None
    buckets: Counter[str] = Counter()
    sample: dict[str, dict[str, Any]] = {}
    for h in hits:
        t, m, _ = _error_fields(h, kind)
        key = f"{t or '?'}: {_normalise(m)}"
        buckets[key] += 1
        sample.setdefault(key, h)
    key, n = buckets.most_common(1)[0]
    if n / len(hits) < share:
        return None, sample[key]
    return key, sample[key]


def _stack_to_exception(sig: str | None, stack: str | None) -> Exception_ | None:
    if not sig:
        return None
    t, _, v = sig.partition(": ")
    return Exception_(type=t or "Error", value=scrub_text(v) or "")


def _explorer_link(
    app: str, path: str, query: str, frm: datetime, to: datetime, k_from: str, k_to: str
) -> HttpUrl:
    q = httpx.QueryParams({"query": query})
    return HttpUrl(
        f"https://{app}/{path}?{q}&{k_from}={int(frm.timestamp() * 1000)}"
        f"&{k_to}={int(to.timestamp() * 1000)}"
    )


def build_evidence(
    event: ErrorEvent,
    monitor: dict[str, Any],
    kind: MonitorKind,
    query: str,
    hits: list[dict[str, Any]],
    sig: str | None,
    sample: dict[str, Any] | None,
    frm: datetime,
    to: datetime,
    site: str,
) -> Evidence:
    _, _, stack = _error_fields(sample, kind) if sample else (None, None, None)
    sample_logs: list[str] = []
    if stack:
        sample_logs.extend(scrub_text(line) or "" for line in str(stack).splitlines()[:40])
    elif sample:
        msg = _attr(sample, "message") or (sample.get("attributes") or {}).get("message")
        if msg:
            sample_logs.append(scrub_text(str(msg)) or "")
    hints = [f"monitor: {monitor.get('name', '')}".strip()]
    scope = parse_scope(event.source_ids.get("scope"))
    if scope.get("resource_name"):
        hints.append(f"endpoint: {scope['resource_name']}")
    for k in ("cluster", "service", "env"):
        if scope.get(k):
            hints.append(f"{k}: {scope[k]}")
    app = "app." + site
    links: dict[str, HttpUrl] = {}
    if event.url:
        links["monitor_event"] = event.url
    if kind == "log":
        links["logs"] = _explorer_link(app, "logs", query, frm, to, "from_ts", "to_ts")
    elif kind == "spans":
        links["traces"] = _explorer_link(app, "apm/traces", query, frm, to, "start", "end")
    message = scrub_text(event.source_ids.get("message") or monitor.get("message"))
    return Evidence(
        exception=_stack_to_exception(sig, stack),
        signature=sig,
        message=message,
        first_seen=frm if hits else None,
        last_seen=to if hits else None,
        event_count=len(hits) or None,
        sample_logs=sample_logs,
        links=links,
        routing_hints=[h for h in hints if h and not h.endswith(": ")],
    )
