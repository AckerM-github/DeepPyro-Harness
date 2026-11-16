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

Session log envelope
--------------------
deeppyro.session.events validates a raw JSON line into SessionEvent, the
in-memory form whose sourceEventSeqs are flattened to plain ints;
deeppyro.session.writer turns a LogRecord back into SessionEventLine, the
on-disk form whose sourceEventSeqs use [start, end] range pairs.  The first
line of every session log is a SessionHeaderLine.

Hook records and builtin payloads
---------------------------------
deeppyro.hooks.records shapes hook observations into HookInvokedRecord and
HookResultRecord, the two deeppyro-specific record types the builtin hook
journal writes.  The session title plugin stores its one-shot LLM request as
SessionTitleLlmRequestData, and the approval/request waterfall carries the
ApprovalRequestHookPayload that deeppyro.builtin.approval_policy consumes.
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
    "HookInvokedRecord",
    "HookRecordFields",
    "HookResultRecord",
    "HookScope",
    "PostExecuteDecision",
    "PostExecuteHookPayload",
    "PreExecuteDecision",
    "PreExecuteHookPayload",
    "SessionEvent",
    "SessionEventLine",
    "SessionHeaderLine",
    "SessionReplaceSurface",
    "SessionTitleLlmRequestData",
    "SessionTitleRoute",
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


class SessionReplaceSurface(TypedDict):
    """The replace variant of the surfaceOp envelope key."""

    op: Literal["replace"]
    startSeq: int
    endSeq: int


class SessionEvent(TypedDict):
    """A session record envelope in its validated in-memory form.

    sourceEventSeqs is flattened to plain ints here: range pairs are decoded
    when the record enters the log (deeppyro.session.events.normalize_event).
    """

    type: str
    seq: int
    time: int
    data: dict[str, Any]
    surfaceOp: NotRequired[Literal["append"] | SessionReplaceSurface]
    sourceEventSeqs: NotRequired[list[int]]
    ignorable: NotRequired[Literal[True]]


class SessionEventLine(TypedDict):
    """One non-header session log line as persisted to disk.

    Unlike SessionEvent, sourceEventSeqs keeps DSH's compact [start, end]
    range pairs (deeppyro.session.events.encode_source_event_seqs).
    """

    type: str
    seq: int
    time: int
    data: dict[str, Any]
    surfaceOp: NotRequired[Literal["append"] | SessionReplaceSurface]
    sourceEventSeqs: NotRequired[list[int | list[int]]]
    ignorable: NotRequired[Literal[True]]


class SessionHeaderLine(TypedDict):
    """Line 0 of a session.v4.jsonl log (the session header)."""

    type: Literal["session"]
    version: Literal[4]
    id: str
    createdAt: int
    cwd: NotRequired[str]
    parentSession: NotRequired[str]
    isSeeded: bool
    origin: NotRequired[Literal["subagent"]]
    delegationDepth: int
    agentPreset: NotRequired[str]


class HookRecordFields(TypedDict):
    """Fields shared by the two hook journal record types.

    deeppyro.hooks.records copies only primitive payload values, and only the
    ones present, so everything except hook and mode is optional.  callId is
    read from the nested call of a tool hook payload, not from the top level.
    """

    hook: str
    mode: str
    scope: NotRequired[HookScope]
    turn: NotRequired[int]
    step: NotRequired[int]
    tool: NotRequired[str]
    callId: NotRequired[str]
    provider: NotRequired[str]
    model: NotRequired[str]
    phase: NotRequired[str]
    status: NotRequired[str]


class HookInvokedRecord(HookRecordFields):
    """The hook/invoked session record written by the builtin hook journal."""


class HookResultRecord(HookRecordFields):
    """The hook/result session record written by the builtin hook journal.

    outcome is "error" when the dispatch raised, "none" when it produced no
    value, and "value" otherwise; durationMs is the integer dispatch duration.
    valueType and preview describe a non-string result; error carries the
    exception name and message.
    """

    ok: bool
    outcome: Literal["error", "none", "value"]
    durationMs: int
    valueType: NotRequired[str]
    preview: NotRequired[str]
    error: NotRequired[dict[str, Any]]


class SessionTitleRoute(TypedDict):
    """The provider route embedded in a session/title-llm-request payload."""

    provider: str
    model: str


class SessionTitleLlmRequestData(TypedDict):
    """Data of the session/title-llm-request session record.

    deeppyro.builtin.session_title records one small title request; messages are
    the JSON form of deeppyro.core.wire.Message steps and maxTokens comes from
    the [title] options.
    """

    titleProvider: Literal["session-title-first-prompt-llm"]
    messageSeqs: list[int]
    route: SessionTitleRoute
    system: str
    messages: list[dict[str, Any]]
    maxTokens: int
    reasoningEffort: Literal["off"]


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
