"""Severity, confidence and the paging decision. Deterministic; no model call in v1.

Confidence answers "is this a code defect we can fix from the repo?" — it gates the PR.
Severity answers "how bad is it?" — it gates paging, via `policy.page_when`.
Both are explained in `Triage.rationale` so the Slack message can say why.
"""

from __future__ import annotations

import re

from grumpycat.core.config import Config, PageWhen
from grumpycat.core.models import ErrorEvent, Evidence, Severity, Transition, Triage

# Signatures that are almost never fixed by editing application code.
_INFRA_PATTERNS = re.compile(
    r"(?i)\b("
    r"OOM|out of memory|ENOSPC|disk full|SIGKILL|SIGTERM|killed by signal|"
    r"ECONNREFUSED|EHOSTUNREACH|ETIMEDOUT|connection refused|name resolution|"
    r"ProvisionedThroughputExceeded|ThrottlingException|Throttled|rate limit|"
    r"CircuitBreakerOpen|upstream.*(unavailable|timed? ?out)|502|503|504"
    r")\b"
)


def _infra_like(text: str | None) -> bool:
    return bool(text and _INFRA_PATTERNS.search(text))


def confidence(event: ErrorEvent, evidence: Evidence) -> tuple[float, list[str]]:
    """0..1 belief that an engine can fix this from the repository. Returns (score, reasons)."""
    score = 0.2
    why: list[str] = []
    exc = evidence.exception
    if exc is not None:
        score += 0.3
        why.append(f"exception {exc.type}")
        if any(f.in_app for f in exc.frames):
            score += 0.2
            why.append("in-app stack frame")
    elif evidence.signature:
        score += 0.2
        why.append("dominant error signature")
    else:
        why.append("no exception or dominant signature")
    if evidence.sample_logs and exc is None:
        score += 0.05
    if event.transition is Transition.REGRESSION:
        score += 0.1
        why.append("regression of a previously fixed issue")
    if _infra_like(evidence.signature) or (exc and _infra_like(exc.value)):
        score -= 0.35
        why.append("looks infrastructural (throttling / network / resources)")
    if evidence.message and re.search(
        r"(?i)nothing will retry|no retry|not retried", evidence.message
    ):
        score += 0.15  # the alert author is pointing at missing code, not a bad host
        why.append("alert text points at missing retry logic")
    return max(0.0, min(1.0, round(score, 2))), why


def severity(event: ErrorEvent, evidence: Evidence) -> tuple[Severity, list[str]]:
    why: list[str] = []
    level = (event.tags.get("level") or "").lower()
    users = evidence.user_count or 0
    count = evidence.event_count or 0
    if level == "fatal":
        why.append("level fatal")
        return Severity.CRITICAL, why
    if users >= 100 or count >= 1000:
        why.append(f"{users} users / {count} events")
        return Severity.CRITICAL, why
    if users >= 20 or count >= 200 or event.source == "datadog":
        why.append(f"{users} users / {count} events" if users or count else "monitor-driven")
        return Severity.HIGH, why
    if users >= 1 or count >= 10:
        why.append(f"{users} users / {count} events")
        return Severity.MEDIUM, why
    why.append("low volume")
    return Severity.LOW, why


def _env_matches(env: str | None, wanted: str | list[str]) -> bool:
    if env is None:
        return False
    options = [wanted] if isinstance(wanted, str) else wanted
    return any(env == w or env.startswith(w) for w in options)


def should_page(event: ErrorEvent, evidence: Evidence, sev: Severity, rule: PageWhen) -> bool:
    if not _env_matches(event.env, rule.env):
        return False
    if rule.level_fatal and (event.tags.get("level") or "").lower() == "fatal":
        return True
    if rule.users_15m is not None and (evidence.user_count or 0) >= rule.users_15m:
        return True
    if rule.event_count_15m is not None and (evidence.event_count or 0) >= rule.event_count_15m:
        return True
    return sev is Severity.CRITICAL


def triage(event: ErrorEvent, evidence: Evidence, config: Config) -> Triage:
    conf, conf_why = confidence(event, evidence)
    sev, sev_why = severity(event, evidence)
    page = should_page(event, evidence, sev, config.policy.page_when)
    rationale = "; ".join([*sev_why, *conf_why, "page" if page else "no page"])
    return Triage(severity=sev, confidence=conf, page=page, rationale=rationale)
