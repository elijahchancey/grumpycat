"""Grumpycat: production errors in, rule-abiding draft pull requests out."""

from importlib.metadata import PackageNotFoundError, version

PLUGIN_API_VERSION = 1
"""Bump only on a breaking change to the plugin base classes or `PluginSpec`."""

try:
    __version__ = version("grumpycat")
except PackageNotFoundError:  # running from a source checkout without an install
    __version__ = "0.0.0"

__all__ = ["PLUGIN_API_VERSION", "__version__"]
