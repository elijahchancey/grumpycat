"""Helpers for API Gateway v2 (HTTP API) proxy events."""

from __future__ import annotations

import base64
import json
from typing import Any


def is_http(event: dict[str, Any]) -> bool:
    return "rawPath" in event or "routeKey" in event


def path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def headers(event: dict[str, Any]) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def body(event: dict[str, Any]) -> bytes:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return raw.encode() if isinstance(raw, str) else bytes(raw)


def respond(status: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload or {}),
    }
