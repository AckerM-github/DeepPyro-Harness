"""Public plugin API for DeepPyro (API v3).

An external plugin imports only this package and the protocol types it needs:

    from deeppyro.api import Plugin, PluginContext

Everything else (tool base classes, wire models) is available from the regular
deeppyro modules.
"""

from __future__ import annotations

from .capabilities import (
    LIVE_CONFIG_TOKENS,
    SANDBOX_MODES,
    CapabilityManifest,
    RiskLevel,
    ServiceRequirement,
    TokenCost,
    ToolCapability,
    build_manifest,
    is_live_config_key,
    live_config_fields,
    tool_capability,
)
from .context import PluginContext
from .errors import PluginConfigError, PluginDependencyError, PluginError
from .kernel_types import FiberState
from .plugin import Plugin, Service, inject_names, invoke_setup, resolve_injections
from .services import (
    ServiceBag,
    ServiceNotFoundError,
    ServiceTypeError,
    bind_service_type,
    lookup_service,
    require_service,
)
from .types import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_NATIVE,
    AGENT_PRESENTATION_PTC,
    CHILD_DISABLED_TOOLS,
    CORDIS_PRESET_ID,
    DEFAULT_PRESET_ID,
    LEGACY_DEFAULT_PRESET_ID,
    MINIMAL_PRESET_ID,
    PLUGIN_AUTHORING_TOOL_NAMES,
    PTC_PRESET_ID,
    SUBAGENT_TOOL_NAMES,
    TEAM_MODE_DISABLED_TOOLS,
    TEAM_PRESET_ID,
    TEAM_TOOL_NAMES,
    CommandSpec,
    PluginConfig,
    PluginStatus,
    PresetPluginRow,
    PresetSpec,
    PromptSection,
    ProviderContribution,
    ToolContribution,
)
from .version import API_VERSION

__all__ = [
    "AGENT_MODE_DEFAULT",
    "AGENT_MODE_TEAM",
    "AGENT_PRESENTATION_NATIVE",
    "AGENT_PRESENTATION_PTC",
    "API_VERSION",
    "CHILD_DISABLED_TOOLS",
    "CORDIS_PRESET_ID",
    "DEFAULT_PRESET_ID",
    "LEGACY_DEFAULT_PRESET_ID",
    "LIVE_CONFIG_TOKENS",
    "MINIMAL_PRESET_ID",
    "PLUGIN_AUTHORING_TOOL_NAMES",
    "PTC_PRESET_ID",
    "SANDBOX_MODES",
    "SUBAGENT_TOOL_NAMES",
    "TEAM_MODE_DISABLED_TOOLS",
    "TEAM_PRESET_ID",
    "TEAM_TOOL_NAMES",
    "CapabilityManifest",
    "CommandSpec",
    "FiberState",
    "Plugin",
    "PluginConfig",
    "PluginConfigError",
    "PluginContext",
    "PluginDependencyError",
    "PluginError",
    "PluginStatus",
    "PresetPluginRow",
    "PresetSpec",
    "PromptSection",
    "ProviderContribution",
    "RiskLevel",
    "Service",
    "ServiceBag",
    "ServiceNotFoundError",
    "ServiceRequirement",
    "ServiceTypeError",
    "TokenCost",
    "ToolCapability",
    "ToolContribution",
    "bind_service_type",
    "build_manifest",
    "inject_names",
    "invoke_setup",
    "is_live_config_key",
    "live_config_fields",
    "lookup_service",
    "require_service",
    "resolve_injections",
    "tool_capability",
]
