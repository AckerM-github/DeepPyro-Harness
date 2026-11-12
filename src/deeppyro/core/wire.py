"""Frozen wire models for DSH session log v4 and LLM messages.

Field names are the exact on-disk camelCase names from DSH session.v4.jsonl.
Do not rename them: the log is a compatibility contract.
"""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class WireModel(BaseModel):
    """Base model: tolerate unknown fields so newer DSH records still load."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


# --------------------------------------------------------------------------
# Content parts (message.content[])
# --------------------------------------------------------------------------


class TextPart(WireModel):
    type: Literal["text"] = "text"
    text: str = ""


class ReasoningPart(WireModel):
    type: Literal["reasoning"] = "reasoning"
    text: str = ""


class ToolCallPart(WireModel):
    type: Literal["tool-call"] = "tool-call"
    id: str
    name: str
    arguments: str = "{}"


class ImagePart(WireModel):
    type: Literal["image"] = "image"
    attachment: str | dict[str, Any]


class ToolAdditionPart(WireModel):
    type: Literal["tool-addition"] = "tool-addition"
    toolName: str


class ToolRemovalPart(WireModel):
    type: Literal["tool-removal"] = "tool-removal"
    toolName: str


class UnknownPart(WireModel):
    """Forward-compatibility catch-all for content parts this port does not model."""

    type: str


ContentPart = Union[
    TextPart,
    ReasoningPart,
    ToolCallPart,
    ImagePart,
    ToolAdditionPart,
    ToolRemovalPart,
    UnknownPart,
]


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


class Message(WireModel):
    role: Literal["system", "user", "assistant", "tool", "developer"]
    content: list[ContentPart] = Field(default_factory=list)
    id: str = ""
    source: dict[str, Any] | None = None
    # tool messages only
    toolCallId: str | None = None
    isError: bool | None = None

    # -- helpers -----------------------------------------------------------
    def text(self) -> str:
        return "".join(p.text for p in self.content if isinstance(p, TextPart))

    def reasoning(self) -> str:
        return "".join(p.text for p in self.content if isinstance(p, ReasoningPart))

    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.content if isinstance(p, ToolCallPart)]


# --------------------------------------------------------------------------
# Session log records
# --------------------------------------------------------------------------


class SessionHeader(WireModel):
    """First line of a session.v4.jsonl file (no seq/time/data)."""

    type: Literal["session"] = "session"
    version: int = 4
    id: str
    createdAt: int
    cwd: str
    isSeeded: bool = False
    delegationDepth: int = 0
    agentPreset: str | None = None
    parentSession: str | None = None
    origin: str | None = None


class ReplaceOp(WireModel):
    """surfaceOp object variant: replace the surface range [startSeq, endSeq]."""

    op: Literal["replace"] = "replace"
    startSeq: int
    endSeq: int


class LogRecord(WireModel):
    """Every non-header line: {"type", "seq", "time", "data", ...extra envelope keys}.

    Envelope keys (top-level, beside type/seq/time/data):
    - surfaceOp: "append" | {"op": "replace", "startSeq": int, "endSeq": int}
    - sourceEventSeqs: list[int] (surface records other than assistant/message)
    - ignorable: true for unknown event types a reader may skip
    """

    type: str
    seq: int
    time: int = 0
    data: dict[str, Any] = Field(default_factory=dict)
    surfaceOp: Literal["append"] | ReplaceOp | None = None
    sourceEventSeqs: list[int] | None = None
    ignorable: bool | None = None

    def is_surface_event(self) -> bool:
        return self.type in SURFACE_EVENT_TYPES and self.surfaceOp is not None

    def replacement(self) -> ReplaceOp | None:
        return self.surfaceOp if isinstance(self.surfaceOp, ReplaceOp) else None


# Authoritative DSH event vocabulary: `KNOWN_SESSION_EVENT_TYPES` from
# @deepseek-ai/dsh-session/lib/types/known-event-types.js (57 types). The read
# path refuses unknown types unless the record carries envelope `ignorable: true`.
RECORD_TYPES = (
    "agent-preset/selected",
    "agent/inbox/spliced",
    "approval/asked",
    "approval/decided",
    "approval/policy",
    "assistant/attempt",
    "assistant/message",
    "command/done",
    "command/run",
    "compaction/end",
    "compaction/prune",
    "compaction/start",
    "compaction/summary",
    "deliverables/presented",
    "developer/message",
    "feedback/message-delete",
    "feedback/message-put",
    "feedback/record",
    "goal/change",
    "hook/invoked",
    "hook/result",
    "image/offload",
    "job/registered",
    "job/settled",
    "llm/retry",
    "llm/retry-started",
    "model/selection",
    "permission/preset",
    "plan/mode",
    "request/context",
    "request/header",
    "sandbox/mode",
    "schedule/change",
    "session-log-deepseek/delivery-accepted",
    "session/end-seed",
    "session/title",
    "session/title-llm-request",
    "step/end",
    "step/start",
    "subagent/catalog",
    "subagent/descriptor",
    "subagent/model-selection-policy",
    "system/message",
    "team/member",
    "team/message/delivered",
    "team/message/queued",
    "team/task",
    "todo/write",
    "tool-workflow/agent-end",
    "tool-workflow/agent-start",
    "tool-workflow/run-end",
    "tool-workflow/run-start",
    "tool/call",
    "tool/ptc-dispatch",
    "tool/ptc-dispatch-start",
    "tool/result",
    "turn/end",
    "turn/start",
    "user/message",
    "web/deepseek-search-llm-request",
    "working-directory/change",
    "workspace/changes",
)

# Event types whose model-visible effect requires a dedicated pure projection.
MESSAGE_PROJECTION_TYPES = ("image/offload",)

# Records that produce model-visible messages: these MUST carry a surfaceOp.
SURFACE_EVENT_TYPES = (
    "system/message",
    "developer/message",
    "user/message",
    "assistant/message",
    "tool/result",
)
