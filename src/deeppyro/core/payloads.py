"""Typed shapes for the JSON payloads that cross module boundaries.

The project deliberately passes plain dictionaries between layers: the tool
pipeline, the hook bus, session records and the builtin plugins all exchange
dicts whose keys belong to a frozen on-disk or wire format.  Those dicts used to
be typed as dict[str, Any], so a misspelled key only failed at runtime; the
TypedDicts here name the shapes once so a static type checker can catch a key
typo at the construction site or at the consumer.

Rules for this module:

* typing only - no runtime validation, no runtime imports of domain modules
  (the few type dependencies live behind TYPE_CHECKING);
* key names, casing and nesting mirror the wire format exactly.  The insertion
  order of the dict literals at the construction sites is part of the
  serialized bytes and must not change; TypedDicts describe shapes, not order;
* optional keys are NotRequired (or the TypedDict is total=False) so producers
  can keep omitting them.

Tool pipeline
-------------
deeppyro.tools.pipeline builds the three tools/* hook payloads and the
wire-shaped tool call, which deeppyro.agent.session consumes to persist the
tool/call record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NotRequired, Required, TypedDict

if TYPE_CHECKING:
    from deeppyro.core.protocols import ToolResult

__all__ = [
    "AnyToolHookPayload",
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
    """The wire-shaped tool call shared by every tool hook payload."""

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
