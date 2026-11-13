"""Public plugin API for DeepPyro.

An external plugin imports only this package and the protocol types it needs:

    from deeppyro.api import Plugin, PluginContext

Everything else (tool base classes, wire models) is available from the regular
deeppyro modules.
"""

from __future__ import annotations

from .context import PluginContext
from .errors import PluginConfigError, PluginDependencyError, PluginError
from .plugin import Plugin, Service
from .types import (
    CommandSpec,
    PluginStatus,
    PresetPluginRow,
    PresetSpec,
    PromptSection,
    ProviderContribution,
    ToolContribution,
)

API_VERSION = 1

__all__ = [
    "API_VERSION",
    "CommandSpec",
    "Plugin",
    "PluginConfigError",
    "PluginContext",
    "PluginDependencyError",
    "PluginError",
    "PluginStatus",
    "PresetPluginRow",
    "PresetSpec",
    "PromptSection",
    "ProviderContribution",
    "Service",
    "ToolContribution",
]
