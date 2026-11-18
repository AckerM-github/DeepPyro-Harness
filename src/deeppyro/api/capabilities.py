"""Capability manifests: declarative governance metadata for plugins and tools.

A manifest is the machine-readable statement of what a capability needs and what
it may do: the services it provides and requires (with optional version
constraints), the tools, hooks and events it contributes, its permission
surface and sandbox mode, a coarse risk level, and whether replaying it twice is
safe.  The pydantic config JSON schema and the subset of config fields that may
be hot-applied are part of the manifest too, so hosts can expose plugin
capabilities without importing plugin internals.  Token/cost metadata lets the
accounting service price a plugin's work without importing the plugin.

The dataclasses are frozen and cheap to build.  Plugin classes get a manifest
from :func:`build_manifest`; tools expose risk/idempotent fields (see
:class:`deeppyro.tools.base.BaseTool`) that :func:`tool_capability` folds into
a :class:`ToolCapability`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel

#: Sandbox modes a capability may require (mirrors tools.base.SANDBOX_MODES).
SANDBOX_MODES: tuple[str, ...] = ("workspace-write", "danger-full-access")

#: Config field name fragments that are safe to change while the kernel runs.
#:
#: These are observability/limit knobs rather than topology: changing the log
#: level or a timeout never invalidates registered services, so the kernel can
#: apply them to a live plugin.  Matching is per name word (snake_case and
#: camelCase both understood), never a substring match.
LIVE_CONFIG_TOKENS: tuple[str, ...] = (
    "cache",
    "concurrency",
    "debug",
    "delay",
    "interval",
    "level",
    "log",
    "logging",
    "parallel",
    "quiet",
    "retry",
    "timeout",
    "trace",
    "ttl",
    "verbose",
    "verbosity",
)


class RiskLevel(StrEnum):
    """Coarse blast radius of a capability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _risk(value: RiskLevel | str) -> RiskLevel:
    return value if isinstance(value, RiskLevel) else RiskLevel(str(value))


def _name_words(name: str) -> tuple[str, ...]:
    """Split a config field name into lower-case words (snake_case/camelCase)."""
    words: list[str] = []
    current = ""
    for char in name:
        if char.isupper():
            if current:
                words.append(current)
            current = char.lower()
        elif char.isalnum():
            current += char.lower()
        else:
            if current:
                words.append(current)
            current = ""
    if current:
        words.append(current)
    return tuple(words)


def is_live_config_key(name: str) -> bool:
    """Whether a config field can be applied to a running plugin without restart.

    The whitelist is intentionally conservative: log/trace/debug levels and
    timing/retry/concurrency limits only.  Unknown names are not live.
    """
    return any(word in LIVE_CONFIG_TOKENS for word in _name_words(name))


def live_config_fields(target: type[Any] | None) -> tuple[str, ...]:
    """Config field names of a plugin (or pydantic model) that are runtime-safe.

    `target` may be a plugin class (its `Config` model is inspected) or a
    pydantic model class directly.  Returns an empty tuple when there is no
    config model or no field matches the runtime-safe whitelist.
    """
    if target is None:
        return ()
    model: type[Any] | None = None
    declared_config = getattr(target, "Config", None)
    if isinstance(declared_config, type) and issubclass(declared_config, BaseModel):
        model = declared_config
    elif isinstance(target, type) and issubclass(target, BaseModel):
        model = target
    if model is None:
        return ()
    fields = getattr(model, "model_fields", None)
    if not isinstance(fields, Mapping):
        return ()
    return tuple(str(name) for name in fields if is_live_config_key(str(name)))


def config_schema_for(target: type[Any] | None) -> Mapping[str, Any] | None:
    """JSON schema of a plugin's config model, or None when it has no config."""
    if target is None:
        return None
    model = getattr(target, "Config", None)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        return None
    try:
        schema = model.model_json_schema()
    except Exception:  # noqa: BLE001 - a broken schema must not break the manifest
        return None
    return schema if isinstance(schema, Mapping) else None


@dataclass(frozen=True, slots=True)
class TokenCost:
    """Per-million-token prices used by the cost accounting service."""

    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cached_input_per_mtok: float | None = None

    def estimate(self, *, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0) -> float:
        """Return the USD cost for one usage record (prices are per 1M tokens)."""
        cached = 0 if cached_tokens < 0 else cached_tokens
        cached_price = (
            self.input_per_mtok if self.cached_input_per_mtok is None else self.cached_input_per_mtok
        )
        return (
            input_tokens * self.input_per_mtok + output_tokens * self.output_per_mtok + cached * cached_price
        ) / 1_000_000


@dataclass(frozen=True, slots=True)
class ToolCapability:
    """Manifest row for one tool contributed by a plugin or the kernel."""

    name: str
    risk: RiskLevel = RiskLevel.LOW
    idempotent: bool = False
    parallel_safe: bool = True
    token_cost: TokenCost | None = None


@dataclass(frozen=True, slots=True)
class ServiceRequirement:
    """One required service plus an optional version constraint.

    The constraint uses the same syntax as bundle requirements, for example
    `">=0.2"`, `"==1.4.0"` or `"<2.0"`; an empty string means any version.
    """

    service: str
    version: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.service, str) or not self.service:
            raise ValueError("ServiceRequirement.service must be a non-empty string")

    def to_dict(self) -> dict[str, str]:
        """Render as plain JSON-compatible data."""
        return {"service": self.service, "version": self.version}


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    """Declarative capability statement for one plugin.

    Attributes:
        plugin_id: stable plugin id.
        version: plugin version string.
        services_provided: service names the plugin registers.
        services_required: service names the plugin injects.
        tools: tool capabilities contributed by the plugin.
        hooks: hook/event names the plugin subscribes to or emits.
        permissions: host permissions the plugin needs (for example
            `tools:write` or `process:spawn`).
        sandbox: required sandbox mode, or None when the plugin needs no code
            execution.
        risk: coarse blast radius of the plugin as a whole.
        idempotent: whether mounting/setup can be replayed safely.
        token_cost: optional token/cost metadata for accounting.
        context_window: context window the plugin assumes, when relevant.
        metadata: free-form additive metadata (never required by the host).
        events: event names the plugin emits or observes.
        requires_versions: version constraints for required services.
        config_schema: JSON schema of the plugin config, when it declares one.
        live_config: config fields that may be hot-applied without a restart.
    """

    plugin_id: str
    version: str = "0.0.0"
    services_provided: tuple[str, ...] = ()
    services_required: tuple[str, ...] = ()
    tools: tuple[ToolCapability, ...] = ()
    hooks: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    sandbox: str | None = None
    risk: RiskLevel = RiskLevel.LOW
    idempotent: bool = True
    token_cost: TokenCost | None = None
    context_window: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[str, ...] = ()
    requires_versions: tuple[ServiceRequirement, ...] = ()
    config_schema: Mapping[str, Any] | None = None
    live_config: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.plugin_id, str) or not self.plugin_id:
            raise ValueError("CapabilityManifest.plugin_id must be a non-empty string")
        if self.sandbox is not None and self.sandbox not in SANDBOX_MODES:
            raise ValueError(
                f"CapabilityManifest.sandbox must be one of {SANDBOX_MODES}, got {self.sandbox!r}"
            )

    @property
    def provides(self) -> tuple[str, ...]:
        """Service names this capability provides (alias of services_provided)."""
        return self.services_provided

    @property
    def requires(self) -> tuple[str, ...]:
        """Service names this capability requires (alias of services_required)."""
        return self.services_required

    def version_requirement(self, service: str) -> str:
        """Return the declared version constraint for `service` ('' when none)."""
        for requirement in self.requires_versions:
            if requirement.service == service:
                return requirement.version
        return ""

    def to_dict(self) -> dict[str, Any]:
        """Render the manifest as plain JSON-compatible data (for inspect/tools)."""
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "services_provided": list(self.services_provided),
            "services_required": list(self.services_required),
            "provides": list(self.services_provided),
            "requires": list(self.services_required),
            "requires_versions": {item.service: item.version for item in self.requires_versions},
            "tools": [
                {
                    "name": tool.name,
                    "risk": str(tool.risk),
                    "idempotent": tool.idempotent,
                    "parallel_safe": tool.parallel_safe,
                    "token_cost": _cost_dict(tool.token_cost),
                }
                for tool in self.tools
            ],
            "hooks": list(self.hooks),
            "events": list(self.events),
            "permissions": list(self.permissions),
            "sandbox": self.sandbox,
            "risk": str(self.risk),
            "idempotent": self.idempotent,
            "token_cost": _cost_dict(self.token_cost),
            "context_window": self.context_window,
            "config_schema": dict(self.config_schema) if self.config_schema is not None else None,
            "live_config": list(self.live_config),
            "metadata": dict(self.metadata),
        }


def _cost_dict(cost: TokenCost | None) -> dict[str, Any] | None:
    if cost is None:
        return None
    return {
        "input_per_mtok": cost.input_per_mtok,
        "output_per_mtok": cost.output_per_mtok,
        "cached_input_per_mtok": cost.cached_input_per_mtok,
    }


def tool_capability(tool: Any) -> ToolCapability:
    """Fold a tool instance/class into its manifest row."""
    name = str(getattr(tool, "name", "") or "")
    return ToolCapability(
        name=name,
        risk=_risk(getattr(tool, "risk", RiskLevel.LOW)),
        idempotent=bool(getattr(tool, "idempotent", False)),
        parallel_safe=bool(getattr(tool, "parallel_safe", True)),
    )


def _declared_names(value: Any) -> tuple[str, ...]:
    """Normalize a declared string-list class attribute (str/iterable, not methods)."""
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Iterable):
        return tuple(str(item) for item in value if item)
    return ()


def _declared_requirements(cls: type[Any]) -> tuple[ServiceRequirement, ...]:
    declared = getattr(cls, "requires_versions", None)
    if not isinstance(declared, Mapping):
        return ()
    requirements: list[ServiceRequirement] = []
    for service, constraint in declared.items():
        name = str(service)
        version = str(constraint) if constraint is not None else ""
        if name:
            requirements.append(ServiceRequirement(service=name, version=version))
    return tuple(requirements)


def build_manifest(plugin: type[Any] | Any) -> CapabilityManifest:
    """Build the default manifest for a plugin class or instance.

    Plugins may override :meth:`deeppyro.api.plugin.Plugin.capability_manifest`
    to refine the result (for example to list contributed tools).
    """
    cls = plugin if isinstance(plugin, type) else type(plugin)

    # Local import: the plugin module imports this module (api below api.plugin).
    from .plugin import inject_names

    provided: tuple[str, ...] = ()
    declared_provide = getattr(cls, "provide", "")
    if isinstance(declared_provide, str) and declared_provide:
        provided = (declared_provide,)
    try:
        required = tuple(inject_names(cls))
    except Exception:  # noqa: BLE001 - a broken inject list is reported at mount time
        required = ()
    tool_fields: tuple[ToolCapability, ...] = ()
    declared_tools = getattr(cls, "tool_capabilities", None)
    if callable(declared_tools):
        tool_fields = tuple(cast("Iterable[ToolCapability]", declared_tools()))
    declared_hooks = getattr(cls, "hooks", ())
    declared_events = getattr(cls, "events", ())
    declared_volatile = getattr(cls, "volatile", ())
    live: list[str] = []
    if isinstance(declared_volatile, str):
        live.append(declared_volatile)
    elif isinstance(declared_volatile, Iterable):
        live.extend(str(name) for name in declared_volatile if isinstance(name, str) and name)
    for name in live_config_fields(cls):
        if name not in live:
            live.append(name)
    return CapabilityManifest(
        plugin_id=str(getattr(cls, "plugin_id", "") or cls.__name__),
        version=str(getattr(cls, "version", "0.0.0") or "0.0.0"),
        services_provided=provided,
        services_required=required,
        tools=tool_fields,
        hooks=_declared_names(declared_hooks),
        permissions=_declared_names(getattr(cls, "permissions", ())),
        sandbox=getattr(cls, "sandbox", None),
        risk=_risk(getattr(cls, "risk", RiskLevel.LOW)),
        idempotent=bool(getattr(cls, "idempotent", True)),
        token_cost=getattr(cls, "token_cost", None),
        context_window=getattr(cls, "context_window", None),
        metadata={"module": cls.__module__, "qualname": cls.__qualname__},
        events=_declared_names(declared_events),
        requires_versions=_declared_requirements(cls),
        config_schema=config_schema_for(cls),
        live_config=tuple(live),
    )


__all__ = [
    "LIVE_CONFIG_TOKENS",
    "SANDBOX_MODES",
    "CapabilityManifest",
    "RiskLevel",
    "ServiceRequirement",
    "TokenCost",
    "ToolCapability",
    "build_manifest",
    "config_schema_for",
    "is_live_config_key",
    "live_config_fields",
    "tool_capability",
]
