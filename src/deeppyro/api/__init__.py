"""Public plugin API for DeepPyro.

An external plugin imports only this package and the protocol types it needs:

    from deeppyro.api import Plugin, PluginContext

Everything else (tool base classes, wire models) is available from the regular
deeppyro modules.
"""

from __future__ import annotations

from .context import PluginContext
from .errors import PluginConfigError, PluginDependencyError, PluginError
from .kernel_types import FiberState
from .plugin import Plugin, Service
from .types import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    CHILD_DISABLED_TOOLS,
    DEFAULT_PRESET_ID,
    SUBAGENT_TOOL_NAMES,
    TEAM_MODE_DISABLED_TOOLS,
    TEAM_PRESET_ID,
    TEAM_TOOL_NAMES,
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
    "AGENT_MODE_DEFAULT",
    "AGENT_MODE_TEAM",
    "API_VERSION",
    "CHILD_DISABLED_TOOLS",
    "DEFAULT_PRESET_ID",
    "SUBAGENT_TOOL_NAMES",
    "TEAM_MODE_DISABLED_TOOLS",
    "TEAM_PRESET_ID",
    "TEAM_TOOL_NAMES",
    "CommandSpec",
    "FiberState",
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
