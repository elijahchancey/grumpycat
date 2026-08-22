from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from grumpycat.core.models import Transition
from grumpycat.inputs.sentry import SentryConfig, SentryInput, evidence_from_event
from grumpycat.plugins import PluginKind, build
from grumpycat.plugins.spec import InputPlugin

FIX = Path(__file__).parent / "fixtures" / "sentry"
SECRETS = {"SENTRY_WEBHOOK_SECRET": "whsec", "SENTRY_AUTH_TOKEN": "tok"}


def load(name: str) -> dict[str, Any]:
    return json.loads((FIX / name).read_text())


def plugin(**cfg: Any) -> SentryInput:
    return SentryInput(SentryConfig(org="acme", **cfg), SECRETS)


def sign(body: bytes, secret: str = "whsec") -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_is_discoverable_through_the_registry() -> None:
    p = build(PluginKind.INPUT, "sentry", {"org": "acme"}, SECRETS, cls=InputPlugin)
    assert isinstance(p, SentryInput)
    assert set(p.spec.required_secrets) == {"SENTRY_WEBHOOK_SECRET", "SENTRY_AUTH_TOKEN"}


def test_verify_accepts_valid_and_rejects_bad_signatures() -> None:
    body = json.dumps(load("issue_created.json")).encode()
    p = plugin()
    assert p.verify({"Sentry-Hook-Signature": sign(body)}, body)
    assert not p.verify({"sentry-hook-signature": sign(body, "other")}, body)
    assert not p.verify({}, body)
    assert not p.verify({"Sentry-Hook-Signature": sign(body)}, body + b" ")


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("issue_created.json", Transition.NEW),
        ("issue_regressed.json", Transition.REGRESSION),
        ("issue_resolved.json", Transition.RECOVERED),
    ],
)
def test_issue_actions_map_to_transitions(fixture: str, expected: Transition) -> None:
    ev = plugin().parse({**load(fixture), "_resource": "issue"})
    assert ev is not None
    assert ev.transition is expected
    assert ev.fingerprint == "sentry:acme:4512345678"
    assert ev.service == "api"
    assert ev.source_ids["issue_id"] == "4512345678"
    assert str(ev.url) == "https://acme.sentry.io/issues/4512345678/"


def test_uninteresting_actions_and_filters_return_none() -> None:
    assert plugin().parse({**load("issue_assigned.json"), "_resource": "issue"}) is None
    assert plugin(projects=["frontend"]).parse(load("issue_created.json")) is None
    assert plugin(min_level="fatal").parse(load("issue_created.json")) is None
    assert plugin().parse({"action": "created", "data": {}}) is None
    assert plugin().parse({"installation": {}}) is None  # ping / unknown


def test_resource_is_guessed_when_header_missing() -> None:
    ev = plugin().parse(load("issue_created.json"))
    assert ev is not None and ev.transition is Transition.NEW


def test_event_alert_payload_is_new_with_environment() -> None:
    ev = plugin().parse({**load("event_alert.json"), "_resource": "event_alert"})
    assert ev is not None
    assert ev.transition is Transition.NEW
    assert ev.env == "production"
    assert ev.fingerprint == "sentry:acme:4512345678"
    assert ev.source_ids["rule"] == "New prod errors → grumpycat"
    assert ev.tags["release"] == "1333a11ab9c0ffee"
    assert "user" not in ev.tags
    assert plugin(environments=["staging"]).parse(load("event_alert.json")) is None


def test_enrich_fetches_issue_and_latest_event_and_scrubs() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer tok"
        if request.url.path.endswith("/events/latest/"):
            return httpx.Response(200, json=load("api_latest_event.json"))
        return httpx.Response(200, json=load("api_issue.json"))

    p = plugin()
    p.transport = httpx.MockTransport(handler)
    ev = p.parse(load("issue_created.json"))
    assert ev is not None
    evidence = p.enrich(ev)
    assert calls == ["/api/0/issues/4512345678/", "/api/0/issues/4512345678/events/latest/"]
    assert evidence.exception is not None
    assert evidence.exception.type == "NoMethodError"
    assert "jane.doe@example.test" not in evidence.exception.value
    assert "[redacted]" in evidence.exception.value
    assert evidence.exception.frames[-1].filename == "app/services/notifier.rb"
    assert evidence.exception.frames[-1].lineno == 42
    assert evidence.signature is not None and evidence.signature.startswith("NoMethodError: ")
    assert evidence.deployed_sha == "1333a11ab9c0ffee"
    assert evidence.event_count == 412 and evidence.user_count == 57
    assert evidence.request is not None
    assert evidence.request["headers"]["Authorization"] == "[redacted]"
    assert "someone@example.test" not in json.dumps(evidence.request)
    assert "transaction: NotificationsController#create" in evidence.routing_hints
    assert str(evidence.links["sentry"]) == "https://acme.sentry.io/issues/4512345678/"


def test_evidence_from_event_without_exception_entry() -> None:
    ev = evidence_from_event({"firstSeen": "2026-08-21T03:14:07Z"}, {"entries": [], "tags": []})
    assert ev.exception is None and ev.signature is None
    assert ev.first_seen is not None


def test_annotate_posts_a_note() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={})

    p = plugin()
    p.transport = httpx.MockTransport(handler)
    ev = p.parse(load("issue_created.json"))
    assert ev is not None
    p.annotate(ev, None, "PR opened: https://github.com/acme/api/pull/1")  # type: ignore[arg-type]
    assert seen["path"] == "/api/0/issues/4512345678/notes/"
    assert "pull/1" in seen["json"]["text"]
