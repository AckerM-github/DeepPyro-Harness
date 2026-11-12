"""Errors raised by the external plugin manager.

All errors derive from deeppyro.core.errors.DeeppyroError so the TUI can
render them without depending on this package internals.
"""

from __future__ import annotations

from deeppyro.core.errors import DeeppyroError


class PluginError(DeeppyroError):
    """Base error for external plugin management."""


class PluginNotFoundError(PluginError):
    """Raised when a plugin id is not provided by any external source."""


class PluginProtectionError(PluginError):
    """Raised when an operation would modify a built-in bundle plugin."""


class PluginRemovalError(PluginError):
    """Raised when a plugin cannot be removed from the manager."""


class ManifestWriteError(PluginError):
    """Raised when the user plugin manifest cannot be serialized or written."""


__all__ = [
    "ManifestWriteError",
    "PluginError",
    "PluginNotFoundError",
    "PluginProtectionError",
    "PluginRemovalError",
]
