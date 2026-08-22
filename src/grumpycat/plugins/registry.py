"""Discover plugins through entry points and build the ones the config enables.

Startup contract (same in every Lambda and in the worker):

1. Every plugin named in `config.inputs` / `config.engines` / `config.outputs` must resolve to an
   entry point, or startup fails.
2. Its section is validated against the plugin's `config_schema`, or startup fails.
3. Every name in `required_secrets` must be present in the secrets mapping, or startup fails.
4. Every name in `optional_tools` that is not on PATH is logged as a warning, nothing more.
5. A plugin whose `api_version` is not ours is skipped with an error.

Repos reference engines by name (`repos.<repo>.engine`), so engines are built lazily on demand
as well as eagerly when listed under `engines:` (to surface config errors early).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable, Mapping
from importlib.metadata import EntryPoint, entry_points
from typing import Any

from pydantic import ValidationError

from grumpycat import PLUGIN_API_VERSION
from grumpycat.core.config import Config
from grumpycat.plugins.spec import (
    CIPlugin,
    EnginePlugin,
    InputPlugin,
    OutputPlugin,
    Plugin,
    PluginKind,
    PluginSpec,
    Secrets,
)

log = logging.getLogger(__name__)

GROUPS: dict[PluginKind, str] = {
    PluginKind.INPUT: "grumpycat.inputs",
    PluginKind.ENGINE: "grumpycat.engines",
    PluginKind.OUTPUT: "grumpycat.outputs",
    PluginKind.CI: "grumpycat.ci",
}


class PluginError(RuntimeError):
    """Configuration or discovery problem. Message is meant for an operator."""


def discover(kind: PluginKind) -> dict[str, EntryPoint]:
    """All entry points of one kind, by plugin name. Built-ins and third-party alike."""
    return {ep.name: ep for ep in entry_points(group=GROUPS[kind])}


def _load_class(ep: EntryPoint, kind: PluginKind) -> type[Plugin]:
    cls = ep.load()
    spec = getattr(cls, "spec", None)
    if not isinstance(spec, PluginSpec):
        msg = f"{ep.value} has no `spec: PluginSpec`"
        raise PluginError(msg)
    if spec.kind is not kind:
        msg = f"{ep.value} declares kind {spec.kind} but is registered under {GROUPS[kind]}"
        raise PluginError(msg)
    if spec.api_version != PLUGIN_API_VERSION:
        msg = (
            f"{ep.value} targets plugin API v{spec.api_version}; "
            f"this grumpycat speaks v{PLUGIN_API_VERSION}"
        )
        raise PluginError(msg)
    if spec.name != ep.name:
        msg = f"{ep.value} spec.name={spec.name!r} but entry point is {ep.name!r}"
        raise PluginError(msg)
    return cls  # type: ignore[no-any-return]


def _missing_secrets(spec: PluginSpec, secrets: Secrets) -> list[str]:
    return [s for s in spec.required_secrets if not secrets.get(s)]


def _missing_tools(spec: PluginSpec) -> list[str]:
    return [t for t in spec.optional_tools if shutil.which(t) is None]


def build[P: Plugin](
    kind: PluginKind,
    name: str,
    section: Mapping[str, Any] | None,
    secrets: Secrets,
    *,
    cls: type[P],
) -> P:
    """Instantiate one plugin after the full startup contract."""
    eps = discover(kind)
    if name not in eps:
        known = ", ".join(sorted(eps)) or "none"
        msg = f"unknown {kind} plugin {name!r}; installed: {known}"
        raise PluginError(msg)
    plugin_cls = _load_class(eps[name], kind)
    if not issubclass(plugin_cls, cls):
        msg = f"{eps[name].value} is not a {cls.__name__}"
        raise PluginError(msg)
    spec = plugin_cls.spec
    try:
        config = spec.config_schema.model_validate(dict(section or {}))
    except ValidationError as e:
        msg = f"invalid config for {kind} plugin {name!r}:\n{e}"
        raise PluginError(msg) from e
    if missing := _missing_secrets(spec, secrets):
        msg = f"{kind} plugin {name!r} needs secrets not in the secrets map: {missing}"
        raise PluginError(msg)
    if missing_tools := _missing_tools(spec):
        log.warning("%s plugin %r: optional tools not on PATH: %s", kind, name, missing_tools)
    return plugin_cls(config, secrets)


class Registry:
    """All enabled plugins for one config, constructed once per process."""

    def __init__(self, config: Config, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets
        self.inputs: dict[str, InputPlugin] = {
            n: build(PluginKind.INPUT, n, s, secrets, cls=InputPlugin)
            for n, s in config.inputs.items()
        }
        self.outputs: dict[str, OutputPlugin] = {
            n: build(PluginKind.OUTPUT, n, s, secrets, cls=OutputPlugin)
            for n, s in config.outputs.items()
        }
        self.ci: CIPlugin | None = (
            build(PluginKind.CI, config.ci.provider, config.ci.options, secrets, cls=CIPlugin)
            if config.ci
            else None
        )
        self._engines: dict[str, EnginePlugin] = {}
        wanted: Iterable[str] = set(config.engines) | {r.engine for r in config.repos.values()}
        for n in wanted:
            self.engine(n)

    def engine(self, name: str) -> EnginePlugin:
        if name not in self._engines:
            self._engines[name] = build(
                PluginKind.ENGINE,
                name,
                self.config.engines.get(name),
                self.secrets,
                cls=EnginePlugin,
            )
        return self._engines[name]

    @property
    def engines(self) -> Mapping[str, EnginePlugin]:
        return dict(self._engines)

    def event_patterns(self) -> dict[str, dict[str, Any]]:
        """Inputs that want an EventBridge rule, by name. Terraform reads this via the CLI."""
        return {
            n: p.spec.event_pattern
            for n, p in self.inputs.items()
            if p.spec.event_pattern is not None
        }
