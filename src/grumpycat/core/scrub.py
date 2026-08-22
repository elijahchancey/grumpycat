"""PII and secret scrubbing for anything an input puts into `Evidence`.

Conservative by design: we would rather lose a request parameter than ship a customer's email
to a model provider or into a PR body. Inputs call `scrub_text` on free text and `scrub_mapping`
on structured data before returning evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
_BEARER = re.compile(r"(?i)\b(bearer|token|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*")
_LONG_SECRET = re.compile(
    r"\b(?=[A-Za-z0-9_\-]{32,}\b)(?=.*[A-Z])(?=.*[a-z])(?=.*\d)[A-Za-z0-9_\-]{32,}\b"
)
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE = re.compile(r"(?<!\d)\+?\d{1,3}[ -.]?\(?\d{2,4}\)?[ -.]?\d{3,4}[ -.]?\d{3,4}(?!\d)")

SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "session",
        "ssn",
        "credit_card",
        "card_number",
        "cvv",
    }
)

REDACTED = "[redacted]"


def scrub_text(text: str | None) -> str | None:
    if not text:
        return text
    out = _BEARER.sub(r"\1 " + REDACTED, text)
    out = _EMAIL.sub(REDACTED, out)
    out = _IPV4.sub(REDACTED, out)
    out = _IPV6.sub(REDACTED, out)
    out = _CARD.sub(REDACTED, out)
    out = _LONG_SECRET.sub(REDACTED, out)
    return _PHONE.sub(REDACTED, out)


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return k in SENSITIVE_KEYS or any(
        s in k for s in ("password", "secret", "token", "cookie", "api_key", "apikey")
    )


def scrub_value(value: Any, *, depth: int = 6) -> Any:
    """Scrub any JSON-ish value: mappings by key, strings by pattern, lists element-wise."""
    if depth <= 0:
        return REDACTED
    if isinstance(value, Mapping):
        return scrub_mapping(value, depth=depth)
    if isinstance(value, list | tuple):
        return [scrub_value(v, depth=depth - 1) for v in list(value)[:50]]
    if isinstance(value, str):
        return scrub_text(value)
    return value


def scrub_mapping(data: Mapping[str, Any] | None, *, depth: int = 6) -> dict[str, Any] | None:
    """Redact sensitive keys outright and scrub everything else, recursively."""
    if data is None:
        return None
    if depth <= 0:
        return {"...": REDACTED}
    out: dict[str, Any] = {}
    for key, value in data.items():
        k = str(key)
        out[k] = REDACTED if _is_sensitive_key(k) else scrub_value(value, depth=depth - 1)
    return out
