"""Receive an alert (HTTP webhook or EventBridge event), parse it, hand it to triage.

Stays fast: verify → parse (pure) → async-invoke the triage Lambda → 202. Sources time out
webhooks in a few seconds; enrichment happens in triage.
"""

from __future__ import annotations

import json
import os
from typing import Any

from grumpycat.core.models import ErrorEvent
from grumpycat.handlers import http
from grumpycat.handlers.runtime import Runtime, logger, runtime
from grumpycat.plugins.spec import InputPlugin, Trigger

SENTRY_RESOURCE_HEADER = "sentry-hook-resource"


def _dispatch(rt: Runtime, event: ErrorEvent) -> None:
    rt.lam.invoke(
        FunctionName=os.environ["GRUMPYCAT_TRIAGE_FUNCTION"],
        InvocationType="Event",
        Payload=event.model_dump_json().encode(),
    )
    logger.info(
        "dispatched to triage",
        source=event.source,
        fingerprint=event.fingerprint,
        transition=event.transition,
    )


def _handle_http(rt: Runtime, event: dict[str, Any]) -> dict[str, Any]:
    p = http.path(event)
    if not p.startswith("/in/"):
        return http.respond(404, {"error": "not found"})
    name = p.removeprefix("/in/").strip("/")
    plugin = rt.registry.inputs.get(name)
    if plugin is None or plugin.spec.trigger is not Trigger.HTTP:
        return http.respond(404, {"error": f"no http input named {name!r}"})
    hdrs = http.headers(event)
    raw = http.body(event)
    if not plugin.verify(hdrs, raw):
        logger.warning("webhook signature rejected", input=name)
        return http.respond(401, {"error": "invalid signature"})
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return http.respond(400, {"error": "body is not JSON"})
    if not isinstance(payload, dict):
        return http.respond(400, {"error": "body must be a JSON object"})
    payload["_headers"] = hdrs
    if SENTRY_RESOURCE_HEADER in hdrs:
        payload["_resource"] = hdrs[SENTRY_RESOURCE_HEADER]
    parsed = plugin.parse(payload)
    if parsed is None:
        return http.respond(204)
    _dispatch(rt, parsed)
    return http.respond(202, {"fingerprint": parsed.fingerprint, "transition": parsed.transition})


def _handle_eventbridge(rt: Runtime, event: dict[str, Any]) -> dict[str, Any]:
    candidates: list[InputPlugin] = [
        p for p in rt.registry.inputs.values() if p.spec.trigger is Trigger.EVENTBRIDGE
    ]
    for plugin in candidates:
        parsed = plugin.parse(event)
        if parsed is not None:
            _dispatch(rt, parsed)
            return {"accepted": parsed.fingerprint, "input": plugin.spec.name}
    logger.info("eventbridge event matched no input", detail_type=event.get("detail-type"))
    return {"accepted": None}


@logger.inject_lambda_context
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    rt = runtime()
    if http.is_http(event):
        return _handle_http(rt, event)
    if "detail-type" in event:
        return _handle_eventbridge(rt, event)
    logger.warning("unrecognised event shape", keys=sorted(event)[:10])
    return {"accepted": None}
