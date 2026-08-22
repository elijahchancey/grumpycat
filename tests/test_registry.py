from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest

from grumpycat.core.config import load_config
from grumpycat.plugins import PluginError, PluginKind, Registry, build
from grumpycat.plugins.spec import EnginePlugin, InputPlugin
from tests import fakes
from tests.conftest import MINIMAL_YAML

Register = Callable[[PluginKind, dict[str, Any]], None]

SECRETS = {"FAKE_TOKEN": "t"}


def _register_all(register: Register) -> None:
    register(PluginKind.INPUT, {"fake_in": fakes.FakeInput, "fake_events": fakes.FakeEventInput})
    register(PluginKind.ENGINE, {"fake_engine": fakes.FakeEngine})
    register(PluginKind.OUTPUT, {"fake_out": fakes.FakeOutput})


def test_registry_builds_everything_the_config_names(fake_entry_points: Register) -> None:
    _register_all(fake_entry_points)
    reg = Registry(load_config(MINIMAL_YAML), SECRETS)
    assert set(reg.inputs) == {"fake_in"}
    assert set(reg.outputs) == {"fake_out"}
    assert set(reg.engines) == {"fake_engine"}  # pulled in via repos.*.engine
    assert isinstance(reg.inputs["fake_in"], InputPlugin)
    assert isinstance(reg.engine("fake_engine"), EnginePlugin)
    assert reg.inputs["fake_in"].config.site == "example.test"  # type: ignore[attr-defined]


def test_plugin_config_is_validated_against_schema(fake_entry_points: Register) -> None:
    _register_all(fake_entry_points)
    cfg = load_config(MINIMAL_YAML.replace("fake_in: {}", "fake_in: {sight: typo}"))
    with pytest.raises(PluginError, match="invalid config for input plugin 'fake_in'"):
        Registry(cfg, SECRETS)


def test_missing_required_secret_fails_fast(fake_entry_points: Register) -> None:
    _register_all(fake_entry_points)
    with pytest.raises(
        PluginError, match=r"needs secrets not in the secrets map: \['FAKE_TOKEN'\]"
    ):
        Registry(load_config(MINIMAL_YAML), {})


def test_missing_optional_tool_only_warns(
    fake_entry_points: Register, caplog: pytest.LogCaptureFixture
) -> None:
    _register_all(fake_entry_points)
    with caplog.at_level(logging.WARNING, logger="grumpycat.plugins.registry"):
        Registry(load_config(MINIMAL_YAML), SECRETS)
    assert "definitely-not-a-real-cli" in caplog.text


def test_unknown_plugin_lists_installed_ones(fake_entry_points: Register) -> None:
    _register_all(fake_entry_points)
    cfg = load_config(MINIMAL_YAML.replace("fake_in: {}", "pagerduty: {}"))
    with pytest.raises(
        PluginError, match="unknown input plugin 'pagerduty'; installed: fake_events, fake_in"
    ):
        Registry(cfg, SECRETS)


def test_kind_mismatch_is_rejected(fake_entry_points: Register) -> None:
    fake_entry_points(PluginKind.INPUT, {"wrong_kind": fakes.WrongKind})
    with pytest.raises(PluginError, match="declares kind output"):
        build(PluginKind.INPUT, "wrong_kind", {}, SECRETS, cls=InputPlugin)


def test_api_version_mismatch_is_rejected(fake_entry_points: Register) -> None:
    fake_entry_points(PluginKind.ENGINE, {"old_api": fakes.OldApi})
    with pytest.raises(PluginError, match="targets plugin API v0"):
        build(PluginKind.ENGINE, "old_api", {}, SECRETS, cls=EnginePlugin)


def test_class_without_spec_is_rejected(fake_entry_points: Register) -> None:
    fake_entry_points(PluginKind.ENGINE, {"nospec": fakes.NoSpec})
    with pytest.raises(PluginError, match="has no `spec: PluginSpec`"):
        build(PluginKind.ENGINE, "nospec", {}, SECRETS, cls=EnginePlugin)


def test_event_patterns_expose_eventbridge_inputs(fake_entry_points: Register) -> None:
    _register_all(fake_entry_points)
    cfg = load_config(MINIMAL_YAML.replace("fake_in: {}", "fake_in: {}\n  fake_events: {}"))
    reg = Registry(cfg, SECRETS)
    assert reg.event_patterns() == {
        "fake_events": {"source": ["aws.ecs"], "detail-type": ["ECS Task State Change"]}
    }
    assert reg.inputs["fake_events"].verify({}, b"") is True  # trusted by the rule
    assert reg.inputs["fake_in"].verify({"x-fake-sig": "ok"}, b"") is True
    assert reg.inputs["fake_in"].verify({}, b"") is False
