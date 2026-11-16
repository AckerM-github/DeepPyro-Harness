"""Agent-facing tools for the default subagent layer.

The subagent tool resolves the subagent service from ToolContext.services, so
the runtime decides which sessions may delegate.  subagent runs synchronously:
it waits for the child agent's final reply and returns it.  subagent_fork starts
from the parent's last completed turn and runs in the background: it returns an
activation immediately and the parent receives a settlement notice when the
child finishes.  Starting a child agent executes model work in a separate
session and therefore requires human approval.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import OptionalStrippedStr, StrippedStr, ToolArgs


def _subagent(ctx: ToolContext) -> Any:
    service = (ctx.services or {}).get("subagent")
    if service is None:
        raise ToolError("subagent service is not available in this session")
    return service


def _required(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must be a non-empty string")
    return value.strip()


def _optional(args: dict[str, Any], name: str) -> str | None:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{name} must be a string")
    text = value.strip()
    return text or None


class SubagentTool(BaseTool):
    """Delegate one self-contained task to a child agent and wait for its reply."""

    name = "subagent"
    description = (
        "Delegate one self-contained task to a child agent that works in its own session with its "
        "own tool set, then wait for and return the child's final reply. The child cannot delegate "
        "further (no team or subagent tools) and does not share the parent conversation, so the "
        "prompt must be self-contained. This call runs the whole child turn synchronously: it "
        "returns the final reply, not a background handle. Starting a child agent executes model "
        "work and therefore requires human approval."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for the synchronous subagent tool."""

        prompt: StrippedStr = Field(description="self-contained task for the child agent")
        description: StrippedStr = Field(description="short description of the delegated work")
        name: OptionalStrippedStr = Field(
            default=None,
            description="optional child label (defaults to a generated subagent-N name)",
        )
        provider: OptionalStrippedStr = Field(
            default=None,
            description="optional llm provider name (defaults to the current provider)",
        )
        model: OptionalStrippedStr = Field(
            default=None,
            description="optional model id (defaults to the current model)",
        )
        preset: OptionalStrippedStr = Field(
            default=None,
            description="optional agent preset id (defaults to the current preset)",
        )

        @classmethod
        def missing_message(cls, field: str) -> str:
            return f"{field} must be a non-empty string"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _subagent(ctx)
        prompt = _required(args, "prompt")
        description = _required(args, "description")
        try:
            result = await service.run(
                prompt,
                description=description,
                name=_optional(args, "name"),
                provider=_optional(args, "provider"),
                model=_optional(args, "model"),
                preset=_optional(args, "preset"),
            )
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"subagent failed: {exc}", is_error=True)
        meta = {
            "childSessionId": result.session_id,
            "label": result.label,
            "provider": result.provider,
            "model": result.model,
        }
        if result.is_error:
            return ToolResult(text=result.error or "subagent run failed", is_error=True, meta=meta)
        return ToolResult(
            text=f"subagent {result.label} ({result.session_id}) completed:\n{result.text}",
            meta=meta,
        )


class SubagentForkTool(BaseTool):
    """Fork the parent session into a background child agent."""

    name = "subagent_fork"
    description = (
        "Fork the current conversation into a child agent that runs in the background. The fork "
        "inherits exactly the parent turns that were already completed: it does not see the current "
        "unfinished turn. This call returns immediately with an activation handle (the child session "
        "id); when the child finishes, the parent receives a settlement notice with the child's "
        "closing message. Provider, model, and preset are inherited from the parent and cannot be "
        "overridden."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for the forked subagent tool."""

        prompt: StrippedStr = Field(description="self-contained task for the forked child agent")
        description: StrippedStr = Field(description="short description of the delegated work")
        name: OptionalStrippedStr = Field(
            default=None,
            description="optional child label (defaults to a generated subagent-N name)",
        )

        @classmethod
        def missing_message(cls, field: str) -> str:
            return f"{field} must be a non-empty string"

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _subagent(ctx)
        prompt = _required(args, "prompt")
        description = _required(args, "description")
        try:
            activation = await service.start(
                prompt,
                description=description,
                name=_optional(args, "name"),
                mode="fork",
            )
        except Exception as exc:  # noqa: BLE001 - surface as a tool error result
            return ToolResult(text=f"subagent fork failed: {exc}", is_error=True)
        return ToolResult(
            text=json.dumps(
                {"kind": "activation", "subagentId": activation.child_id},
                ensure_ascii=False,
            ),
            meta={
                "childSessionId": activation.child_id,
                "label": activation.label,
                "mode": "fork",
            },
        )


def subagent_tools() -> list[BaseTool]:
    """Return the agent-facing tools of the default subagent layer."""
    return [SubagentTool(), SubagentForkTool()]


__all__ = ["SubagentForkTool", "SubagentTool", "subagent_tools"]
