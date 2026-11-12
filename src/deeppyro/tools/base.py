"""Shared base class for built-in tools.

Every built-in implements the frozen :class:`deeppyro.core.protocols.Tool`
protocol. This base class only supplies the common defaults (empty schema,
parallel-safe) and a stable debug representation; module-level tools still
declare `name`/`description`/`parameters` explicitly so the model-facing
schema is easy to review.
"""

from __future__ import annotations

from typing import Any, ClassVar


class BaseTool:
    """Base implementation of the `Tool` protocol."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    parallel_safe: ClassVar[bool] = True
    parameters: ClassVar[dict[str, Any]] = {}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} parallel_safe={self.parallel_safe}>"


def make_parameters(
    properties: dict[str, Any],
    required: list[str] | None = None,
    *,
    additional_properties: bool = False,
) -> dict[str, Any]:
    """Build a JSON-schema object for a tool's `parameters` field."""
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = list(required)
    schema["additionalProperties"] = additional_properties
    return schema


def sandbox_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Return `properties` plus the one-shot escalation fields DSH exposes.

    DSH only offers `sandbox_permissions`/`justification` on tools that can
    widen the sandbox for a single call (write, edit, pwsh); read-only tools
    omit them.
    """
    merged = dict(properties)
    merged["sandbox_permissions"] = {
        "type": "string",
        "enum": ["workspace-write", "danger-full-access"],
        "description": "Narrowest wider sandbox mode to request for this one call; requires justification "
        "and fresh user approval.",
    }
    merged["justification"] = {
        "type": "string",
        "description": "One sentence explaining to the user why this exact operation needs wider access.",
    }
    return merged
