"""Runtime contracts shared by tools, prompt, PTC and the agent host.

The tool contract is wire-free: :class:`ToolSchema` is a small pydantic model
used by the registry, the prompt renderer and the PTC SDK, while
:class:`ToolContext`, :class:`ToolResult` and :class:`Tool` are plain runtime
types exchanged by the tool pipeline. Session and LLM shapes live in their own
native modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ToolSchema(BaseModel):
    """Declaration of one model-facing tool."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    # Optional typed result schema used by the PTC SDK renderer; never sent to
    # the model as part of the tool declaration itself.
    output: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolResult:
    """Outcome of one tool call."""

    text: str = ""
    is_error: bool = False
    meta: dict[str, Any] | None = None
    # optional non-text parts (images etc.)
    parts: list[dict[str, Any]] = field(default_factory=list)
    # structured failure info persisted as tool/result.data.error, mirroring
    # the reference pipeline's `{name, code}` shape (for example
    # `{"name": "AbortError", "code": TOOL_ABORTED}`).
    error: dict[str, Any] | None = None


@dataclass(slots=True)
class ToolContext:
    """Everything a tool needs to run one call."""

    cwd: Path
    session_id: str
    turn: int
    step: int
    call_id: str
    cancel: asyncio.Event
    log: Callable[[str, dict[str, Any]], None]
    # permission gate object owned by deeppyro.sandbox; declared structurally
    permission: Any | None = None
    # runtime services (jobs manager, session control, ...) attached by the app
    services: dict[str, Any] = field(default_factory=dict)
    # the native agent host executing this call, attached by the agent loop;
    # tools that need the open turn (goal tools) read it instead of guessing
    agent: Any | None = None
    # End the turn once this tool batch returns, instead of asking the model for
    # another step.  The argument is the turn/end reason, e.g.
    # {"kind": "completed"}; goal tools use it after complete/blocked.
    conclude_turn: Callable[[dict[str, Any]], None] | None = None


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    parameters: dict[str, Any]
    parallel_safe: bool

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


__all__ = ["Tool", "ToolContext", "ToolResult", "ToolSchema"]
