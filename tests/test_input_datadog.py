from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from grumpycat.core.models import Transition
from grumpycat.inputs.datadog import (
    WEBHOOK_PAYLOAD_TEMPLATE,
    DatadogConfig,
    DatadogInput,
    classify_monitor,
    dominant_signature,
    filters_from_metric_query,
    parse_scope,
)
from grumpycat.plugins import PluginKind, build
from grumpycat.plugins.spec import InputPlugin

FIX = Path(__file__).parent / "fixtures" / "datadog"
SECRETS = {"DD_WEBHOOK_TOKEN": "tok-123", "DD_API_KEY": "api", "DD_APP_KEY": "app"}


def load(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())


def plugin(**cfg: Any) -> DatadogInput:
    return DatadogInput(DatadogConfig(**cfg), SECRETS)


def mocked(p: DatadogInput, routes: dict[str, Any]) -> list[dict[str, Any]]:
    """Route by path suffix; records request bodies for assertions."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["DD-API-KEY"] == "api"
        body = json.loads(request.content) if request.content else None
        seen.append({"path": request.url.path, "json": body})
        for suffix, payload in routes.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    p.transport = httpx.MockTransport(handler)
    return seen


def test_is_discoverable_and_template_is_valid_json_with_placeholders() -> None:
    p = build(PluginKind.INPUT, "datadog", {}, SECRETS, cls=InputPlugin)
    assert isinstance(p, DatadogInput)
    tpl = json.loads(WEBHOOK_PAYLOAD_TEMPLATE)
    assert tpl["alert_id"] == "$ALERT_ID" and tpl["message"] == "$TEXT_ONLY_MSG"


def test_verify_uses_constant_time_token_compare() -> None:
    p = plugin()
    assert p.verify({"X-Grumpycat-Token": "tok-123"}, b"{}")
    assert p.verify({"x-grumpycat-token": "tok-123"}, b"{}")
    assert not p.verify({"X-Grumpycat-Token": "tok-124"}, b"{}")
    assert not p.verify({}, b"{}")
    assert plugin(token_header="X-Other").verify({"x-other": "tok-123"}, b"")


def test_parse_scope_and_metric_filters() -> None:
    assert parse_scope("resource_name:delete_/v1/a:b,env:prod") == {
        "resource_name": "delete_/v1/a:b",
        "env": "prod",
    }
    assert parse_scope(None) == {}
    q = load("api_monitor_metric.json")["query"]
    assert filters_from_metric_query(q) == {
        "env": "prod-api",
        "service": "api",
        "span.kind": "server",
    }


def test_parse_apm_monitor_payload() -> None:
    ev = plugin().parse(load("webhook_apm_endpoint_5xx.json"))
    assert ev is not None
    assert ev.transition is Transition.NEW
    assert (
        ev.fingerprint
        == "datadog:112043591:resource_name:delete_/v1/widgets/:widget_id/reactions/:reaction_id"
    )
    assert ev.service == "api" and ev.env == "prod-api"
    assert ev.source_ids["monitor_id"] == "112043591"
    assert ev.source_ids["scope"].startswith("resource_name:")
    assert "ignored" in ev.source_ids["message"]
    assert ev.occurred_at.year == 2026
    assert ev.tags["team"] == "platform"


def test_parse_filters_and_transitions() -> None:
    payload = load("webhook_apm_endpoint_5xx.json")
    assert plugin(monitors=[1]).parse(payload) is None
    assert plugin(envs=["staging"]).parse(payload) is None
    assert plugin().parse({**payload, "alert_id": "$ALERT_ID"}) is None  # unrendered template
    rec = plugin().parse(load("webhook_recovered.json"))
    assert rec is not None and rec.transition is Transition.RECOVERED
    assert plugin().parse({**payload, "transition": "Warn"}) is None
    assert plugin().parse({**payload, "transition": "Re-Triggered"}).transition is Transition.UPDATE  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("monitor", "scope", "kind", "must_contain"),
    [
        (
            load("api_monitor_metric.json"),
            {"resource_name": "delete_/v1/w"},
            "spans",
            "@error:true",
        ),
        (
            load("api_monitor_log.json"),
            {"cluster": "prod-api"},
            "log",
            "ProvisionedThroughputExceededException",
        ),
        (
            {
                "type": "trace-analytics alert",
                "query": 'trace-analytics("env:prod service:api").rollup("count") > 5',
            },
            {},
            "spans",
            "service:api @error:true",
        ),
        (
            {"type": "rum alert", "query": 'rum("@type:error service:web").rollup("count") > 5'},
            {},
            "rum",
            "@type:error",
        ),
        (
            {
                "type": "error-tracking alert",
                "query": 'error-tracking-rum("service:web").rollup("count") > 1',
            },
            {"service": "web"},
            "rum",
            "service:web",
        ),
        (
            {"type": "error-tracking alert", "query": 'error-tracking("service:api")'},
            {},
            "spans",
            "@error:true",
        ),
        (
            {"type": "metric alert", "query": "avg(last_5m):avg:system.cpu.user{host:x} > 90"},
            {"host": "x"},
            "unknown",
            "",
        ),
        ({"type": "service check", "query": '"http.can_connect".over("*")'}, {}, "unknown", ""),
    ],
)
def test_classify_monitor(
    monitor: dict[str, Any], scope: dict[str, str], kind: str, must_contain: str
) -> None:
    k, q = classify_monitor(monitor, scope)
    assert k == kind
    assert must_contain in q
    for key, value in scope.items():
        assert f"{key}:{value}" in q or kind == "unknown"


def test_dominant_signature_threshold() -> None:
    hits = load("api_spans_search.json")["data"]
    sig, sample = dominant_signature(hits, "spans", 0.5)
    assert (
        sig
        == "ActiveRecord::RecordNotFound: Couldn't find Reaction with 'id'=# [WHERE widget_id = #]"
    )
    assert sample is not None and sample["id"] == "s1"
    assert dominant_signature(load("api_spans_search_mixed.json")["data"], "spans", 0.5) == (
        None,
        {"id": "a", "attributes": {"attributes": {"error": {"type": "A", "message": "one"}}}},
    )
    assert dominant_signature([], "spans", 0.5) == (None, None)


def test_enrich_apm_monitor_searches_spans_in_alert_window() -> None:
    p = plugin(lookback_minutes=10)
    seen = mocked(
        p,
        {
            "/monitor/112043591": load("api_monitor_metric.json"),
            "/spans/events/search": load("api_spans_search.json"),
        },
    )
    ev = p.parse(load("webhook_apm_endpoint_5xx.json"))
    assert ev is not None
    evidence = p.enrich(ev)
    assert [s["path"] for s in seen] == ["/api/v1/monitor/112043591", "/api/v2/spans/events/search"]
    q = seen[1]["json"]["filter"]["query"]
    assert "service:api" in q and "env:prod-api" in q and "@error:true" in q
    assert "resource_name:delete_/v1/widgets/:widget_id/reactions/:reaction_id" in q
    assert evidence.signature is not None and evidence.signature.startswith(
        "ActiveRecord::RecordNotFound"
    )
    assert (
        evidence.exception is not None and evidence.exception.type == "ActiveRecord::RecordNotFound"
    )
    assert any("reactions_controller.rb:31" in line for line in evidence.sample_logs)
    assert evidence.event_count == 4
    assert (
        "endpoint: delete_/v1/widgets/:widget_id/reactions/:reaction_id" in evidence.routing_hints
    )
    assert evidence.message is not None and "above 10%" in evidence.message
    assert "traces" in evidence.links and "monitor_event" in evidence.links


def test_enrich_log_monitor_uses_the_monitors_own_query_and_scrubs() -> None:
    p = plugin()
    seen = mocked(
        p,
        {
            "/monitor/114522102": load("api_monitor_log.json"),
            "/logs/events/search": load("api_logs_search.json"),
        },
    )
    ev = p.parse(load("webhook_log_monitor.json"))
    assert ev is not None
    evidence = p.enrich(ev)
    q = seen[1]["json"]["filter"]["query"]
    assert "ProvisionedThroughputExceededException" in q and "cluster:prod-api" in q
    assert evidence.signature == "ProvisionedThroughputExceededException: Provisioned rate exceeded"
    assert "jane.doe@example.test" not in json.dumps(evidence.model_dump(mode="json"))
    assert evidence.message is not None and "nothing will retry it" in evidence.message
    assert "logs" in evidence.links


def test_enrich_unknown_monitor_type_yields_no_signature() -> None:
    p = plugin()
    mocked(p, {"/monitor/112043591": {"type": "service check", "query": "x", "name": "svc"}})
    ev = p.parse(load("webhook_apm_endpoint_5xx.json"))
    assert ev is not None
    evidence = p.enrich(ev)
    assert evidence.signature is None and evidence.event_count is None
    assert evidence.routing_hints[0] == "monitor: svc"
