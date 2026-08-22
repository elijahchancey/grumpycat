from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import EntryPoint
from typing import Any

import pytest

from grumpycat.plugins import registry
from grumpycat.plugins.spec import PluginKind

MINIMAL_YAML = """
client: acme
inputs:
  fake_in: {}
outputs:
  fake_out: {}
repos:
  acme/api:
    engine: fake_engine
    default_branch: master
    ci_pipeline: acme/api
    services: [api, api-worker]
  acme/frontend:
    engine: fake_engine
"""


@pytest.fixture
def fake_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[PluginKind, dict[str, Any]], None]:
    """Register in-memory plugin classes as if they were installed entry points.

    Usage: fake_entry_points(PluginKind.INPUT, {"fake_in": FakeInput})
    """
    table: dict[PluginKind, dict[str, EntryPoint]] = {k: {} for k in PluginKind}

    def fake_discover(kind: PluginKind) -> dict[str, EntryPoint]:
        return dict(table[kind])

    monkeypatch.setattr(registry, "discover", fake_discover)

    def register(kind: PluginKind, classes: dict[str, type]) -> None:
        for name, cls in classes.items():
            ep = EntryPoint(
                name=name, value=f"{cls.__module__}:{cls.__qualname__}", group=registry.GROUPS[kind]
            )
            # EntryPoint.load() imports by value; these classes live in test modules, which works
            # as long as they are module-level (not nested in a function).
            table[kind][name] = ep

    return register
