"""Slack interactivity endpoint: the approve / dismiss buttons in gated mode.

Verifies `X-Slack-Signature` (v0 HMAC over `v0:<ts>:<body>`), rejects stale timestamps,
then acts on the button's `action_id` + `value` (the fingerprint).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qs

import httpx

from grumpycat.core.models import IssueStatus
from grumpycat.handlers import http
from grumpycat.handlers.runtime import Runtime, logger, runtime
from grumpycat.handlers.triage import start_fix
from grumpycat.outputs.slack import APPROVE, DISMISS

MAX_SKEW_S = 300


def verify(headers: dict[str, str], body: bytes, secret: str, *, now: float | None = None) -> bool:
    ts = headers.get("x-slack-request-timestamp", "")
    sig = headers.get("x-slack-signature", "")
    if not ts.isdigit() or abs((now or time.time()) - int(ts)) > MAX_SKEW_S:
        return False
    base = b"v0:" + ts.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def _respond(response_url: str | None, text: str) -> None:
    if not response_url:
        return
    try:
        httpx.post(response_url, json={"replace_original": False, "text": text}, timeout=10.0)
    except httpx.HTTPError:
        logger.warning("slack response_url post failed")


def handle(rt: Runtime, payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ignored": "no actions"}
    action = actions[0]
    action_id = action.get("action_id")
    fp = str(action.get("value") or "")
    user = (
        (payload.get("user") or {}).get("username")
        or (payload.get("user") or {}).get("id")
        or "someone"
    )
    response_url = payload.get("response_url")
    state = rt.store.get(fp)
    if state is None:
        _respond(response_url, f"Unknown issue `{fp}`.")
        return {"ignored": "unknown fingerprint"}
    if state.status is not IssueStatus.AWAITING_APPROVAL:
        _respond(response_url, f"Already {state.status}; nothing to do.")
        return {"ignored": f"status {state.status}"}

    if action_id == APPROVE:
        if state.brief is None:
            _respond(response_url, "No brief stored for this issue; cannot start a run.")
            return {"ignored": "no brief"}
        state = start_fix(rt, state, state.brief)
        _respond(response_url, f":hammer_and_wrench: {user} approved — fix run started.")
        return {"started": fp, "execution": state.execution_arn}

    if action_id == DISMISS:
        previous = state
        state = state.model_copy(
            update={"status": IssueStatus.CLOSED, "rationale": f"dismissed by {user} in Slack"}
        )
        for name, output in rt.registry.outputs.items():
            try:
                state = output.on_transition(state, previous, None)
            except Exception:
                logger.exception("output failed", output=name)
        rt.store.put(state)
        _respond(response_url, f"Dismissed by {user}.")
        return {"dismissed": fp}

    return {"ignored": action_id}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    rt = runtime()
    hdrs = http.headers(event)
    raw = http.body(event)
    secret = rt.registry.secrets.get("SLACK_SIGNING_SECRET")
    if not secret or not verify(hdrs, raw, secret):
        return http.respond(401, {"error": "invalid signature"})
    form = parse_qs(raw.decode())
    try:
        payload = json.loads(form.get("payload", ["{}"])[0])
    except json.JSONDecodeError:
        return http.respond(400, {"error": "bad payload"})
    result = handle(rt, payload)
    return http.respond(200, result)
