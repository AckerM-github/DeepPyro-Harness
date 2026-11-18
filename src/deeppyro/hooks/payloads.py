"""Typed shapes for the hook payloads exchanged by the tool pipeline.

The pipeline passes plain dictionaries across hook boundaries: the tools/*
waterfalls see one `call` record, the tool name, the turn/step and the owning
agent, and the approval waterfall sees a request summary. The TypedDicts here
name those shapes once so a static type checker catches a key typo at the
construction or consumption site.

Rules for this module: typing only, no runtime validation, and key names mirror
the dictionaries the runtime actually builds. Optional keys are NotRequired so
producers can keep omitting them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NotRequired, Required, TypedDict

if TYPE_CHECKING:
    from deeppyro.core.protocols import ToolResult

__all__ = [
    "AnyToolHookPayload",
    "ApprovalHookApproval",
    "ApprovalRequestHookPayload",
    "ExecuteDecision",
    "ExecuteHookPayload",
    "HookScope",
    "PostExecuteDecision",
    "PostExecuteHookPayload",
    "PreExecuteDecision",
    "PreExecuteHookPayload",
    "ToolCallPayload",
    "ToolHookContext",
    "ToolHookPayload",
]


class ToolCallPayload(TypedDict):
    """The tool call shared by every tool hook payload."""

    callId: str
    name: str
    arguments: str


class HookScope(TypedDict):
    """Dispatch scope derived from the agent that owns a hook call.

    preset is attached by the agents service and is absent for bare agents.
    """

    session: str
    preset: NotRequired[str]


class ToolHookContext(TypedDict):
    """Context fields mixed into every tool hook payload."""

    agent: Any
    toolSet: NotRequired[list[str]]


class ToolHookPayload(ToolHookContext):
    """Fields shared by the tools/pre-execute, tools/execute and tools/post-execute payloads."""

    call: ToolCallPayload
    tool: str
    turn: int | None
    step: int | None


class PreExecuteHookPayload(ToolHookPayload):
    """Payload of the tools/pre-execute waterfall."""


class ExecuteHookPayload(ToolHookPayload):
    """Payload of the tools/execute waterfall."""

    arguments: dict[str, Any]


class PostExecuteHookPayload(ToolHookPayload):
    """Payload of the tools/post-execute waterfall."""

    result: ToolResult


AnyToolHookPayload = PreExecuteHookPayload | ExecuteHookPayload | PostExecuteHookPayload


class PreExecuteDecision(TypedDict, total=False):
    """Decision returned by the tools/pre-execute waterfall.

    Every valid decision names a kind; reason is optional context for the
    deny/ask/cancel paths.
    """

    kind: Required[Literal["allow", "deny", "ask", "cancel"]]
    reason: object


class ExecuteDecision(TypedDict, total=False):
    """Decision returned by the tools/execute waterfall.

    A ToolResult may replace this object entirely; inside a dict, result alone
    is accepted as a shorthand for {"kind": "result", "result": ...}.
    """

    kind: Literal["result"]
    result: object


class PostExecuteDecision(TypedDict, total=False):
    """Decision returned by the tools/post-execute waterfall.

    block carries optional feedback; accept may replace the result text
    (content) or attach a structured value (value).
    """

    kind: Required[Literal["accept", "block"]]
    feedback: object
    content: object
    value: object


class ApprovalHookApproval(TypedDict):
    """The approval summary embedded in the approval/request hook payload."""

    id: str
    toolName: str
    callId: str | None


class ApprovalRequestHookPayload(TypedDict):
    """Payload of the approval/request waterfall.

    ApprovalService (deeppyro.sandbox.approval) builds it and the builtin
    approval policy consumes it; request is an ApprovalRequest, agent, policy
    and signal are optional services the caller passes through.
    """

    id: str
    request: object
    approval: ApprovalHookApproval
    agent: Any
    policy: Any
    signal: Any
