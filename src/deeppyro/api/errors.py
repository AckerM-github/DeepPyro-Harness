"""Errors raised by the DeepPyro plugin API.

These errors are deliberately small and dependency free so plugin packages can
import them without importing the kernel.
"""

from __future__ import annotations


class PluginError(RuntimeError):
    """Base error for plugin contract violations."""


class PluginConfigError(PluginError, ValueError):
    """A plugin row carried a config value that does not match its schema."""


class PluginDependencyError(PluginError, LookupError):
    """A plugin required a service that is not available in its context."""


__all__ = ["PluginConfigError", "PluginDependencyError", "PluginError"]
