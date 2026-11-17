"""Capability manifests: declarative governance metadata for plugins and tools.

A manifest is the machine-readable statement of what a capability needs and what
it may do: the services it provides and requires, the tools and hooks it
contributes, its permission surface and sandbox mode, a coarse risk level, and
whether replaying it twice is safe.  Token/cost metadata lets the accounting
service price a plugin's work without importing the plugin.

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

#: Sandbox modes a capability may require (mirrors tools.base.SANDBOX_MODES).
SANDBOX_MODES: tuple[str, ...] = ("workspace-write", "danger-full-access")


class RiskLevel(StrEnum):
    """Coarse blast radius of a capability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _risk(value: RiskLevel | str) -> RiskLevel:
    return value if isinstance(value, RiskLevel) else RiskLevel(str(value))


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

    def __post_init__(self) -> None:
        if not isinstance(self.plugin_id, str) or not self.plugin_id:
            raise ValueError("CapabilityManifest.plugin_id must be a non-empty string")
        if self.sandbox is not None and self.sandbox not in SANDBOX_MODES:
            raise ValueError(
                f"CapabilityManifest.sandbox must be one of {SANDBOX_MODES}, got {self.sandbox!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Render the manifest as plain JSON-compatible data (for inspect/tools)."""
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "services_provided": list(self.services_provided),
            "services_required": list(self.services_required),
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
            "permissions": list(self.permissions),
            "sandbox": self.sandbox,
            "risk": str(self.risk),
            "idempotent": self.idempotent,
            "token_cost": _cost_dict(self.token_cost),
            "context_window": self.context_window,
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
    return CapabilityManifest(
        plugin_id=str(getattr(cls, "plugin_id", "") or cls.__name__),
        version=str(getattr(cls, "version", "0.0.0") or "0.0.0"),
        services_provided=provided,
        services_required=required,
        tools=tool_fields,
        permissions=tuple(getattr(cls, "permissions", ()) or ()),
        sandbox=getattr(cls, "sandbox", None),
        risk=_risk(getattr(cls, "risk", RiskLevel.LOW)),
        idempotent=bool(getattr(cls, "idempotent", True)),
        token_cost=getattr(cls, "token_cost", None),
        context_window=getattr(cls, "context_window", None),
        metadata={"module": cls.__module__, "qualname": cls.__qualname__},
    )


__all__ = [
    "SANDBOX_MODES",
    "CapabilityManifest",
    "RiskLevel",
    "TokenCost",
    "ToolCapability",
    "build_manifest",
    "tool_capability",
]
