"""Plugin contract and registry. See `docs/plugins.md`."""

from grumpycat.plugins.registry import PluginError, Registry, build, discover
from grumpycat.plugins.spec import (
    EmptyConfig,
    EnginePlugin,
    InputPlugin,
    OutputPlugin,
    Plugin,
    PluginKind,
    PluginSpec,
    Secrets,
    Trigger,
)

__all__ = [
    "EmptyConfig",
    "EnginePlugin",
    "InputPlugin",
    "OutputPlugin",
    "Plugin",
    "PluginError",
    "PluginKind",
    "PluginSpec",
    "Registry",
    "Secrets",
    "Trigger",
    "build",
    "discover",
]
