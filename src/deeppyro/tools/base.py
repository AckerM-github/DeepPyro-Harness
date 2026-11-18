"""Shared base class for built-in tools.

Every built-in implements the frozen :class:`deeppyro.core.protocols.Tool`
protocol. This base class supplies the common defaults (empty schema,
parallel-safe) and a stable debug representation. Tools declare a nested
`Args` argument model (a :class:`deeppyro.tools.params.ToolArgs` subclass); the
model-facing JSON schema is derived from it at class-creation time. A tool that
wraps a remote or runtime-dependent schema (MCP adapters, the PTC bridge) may
still assign `parameters` explicitly, either on the class or per instance.
"""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api.capabilities import RiskLevel
from deeppyro.tools.params import ToolArgs, derive_parameters


class BaseTool:
    """Base implementation of the `Tool` protocol.

    These are the protocol's instance attributes, not ClassVars: most tools
    declare them as class attributes, but a tool that wraps a remote schema
    (MCP tools, the PTC bridge) fills them in per instance.
    """

    name: str = ""
    description: str = ""
    parallel_safe: bool = True
    #: Coarse blast radius reported by capability manifests; never part of the
    #: model-facing JSON schema.
    risk: RiskLevel = RiskLevel.LOW
    #: Whether repeating the call is safe; never part of the JSON schema.
    idempotent: bool = False
    parameters: dict[str, Any] = {}
    #: Optional JSON schema for the tool's result, used by the PTC SDK renderer.
    output_schema: dict[str, Any] | None = None
    #: Argument model; when set (and "parameters" is not spelled out) the
    #: model-facing schema is derived from it.
    Args: ClassVar[type[ToolArgs] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Give every subclass its own schema dict: the BaseTool one is shared, so a
        # tool that filled it in place would edit every other tool's schema.
        if "parameters" not in cls.__dict__:
            args_model = cls.__dict__.get("Args")
            if isinstance(args_model, type) and issubclass(args_model, ToolArgs):
                cls.parameters = derive_parameters(args_model)
            else:
                cls.parameters = {}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} parallel_safe={self.parallel_safe}>"


#: Sandbox modes a tool may request for one escalated call (upstream order).
SANDBOX_MODES: tuple[str, ...] = ("workspace-write", "danger-full-access")

SANDBOX_PERMISSIONS_DESCRIPTION = (
    "Narrowest wider sandbox mode to request for this one call; requires justification "
    "and fresh user approval."
)

JUSTIFICATION_DESCRIPTION = "One sentence explaining to the user why this exact operation needs wider access."


def make_parameters(
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    additional_properties: bool = False,
) -> dict[str, Any]:
    """Build a JSON-schema object for a tool's `parameters` field.

    Built-in tools derive their schema from an `Args` model; this helper stays
    for tools whose schema is assembled by hand (compaction tools, dynamic
    schemas, plugin-author examples).
    """
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    schema["additionalProperties"] = additional_properties
    return schema


def sandbox_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Return `properties` plus the one-shot escalation fields upstream exposes.

    upstream only offers `sandbox_permissions`/`justification` on tools that can
    widen the sandbox for a single call (write, edit, pwsh); read-only tools
    omit them.
    """
    merged = dict(properties)
    merged["sandbox_permissions"] = {
        "type": "string",
        "enum": list(SANDBOX_MODES),
        "description": SANDBOX_PERMISSIONS_DESCRIPTION,
    }
    merged["justification"] = {
        "type": "string",
        "description": JUSTIFICATION_DESCRIPTION,
    }
    return merged
