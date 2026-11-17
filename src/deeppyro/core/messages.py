"""Native message model shared by sessions, providers, and the fold.

The model is deliberately a frozen slots dataclass: it sits on the hot path and
must stay cheap to construct and compare. Validation happens at the JSON
boundary (from_dict/to_dict) and once in __post_init__ so a message that exists
in memory can never violate the role/part contract.

The shape is frozen by docs/native/schema.md section 6.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

PartType = Literal["text", "reasoning", "tool_call", "image"]
Role = Literal["system", "user", "assistant", "tool", "developer"]
SourceKind = Literal["user", "model", "tool", "system", "goal", "compaction", "repair", "plugin"]
MediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]

ROLES: frozenset[str] = frozenset({"system", "user", "assistant", "tool", "developer"})
PART_TYPES: frozenset[str] = frozenset({"text", "reasoning", "tool_call", "image"})
MEDIA_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
SOURCE_KINDS: frozenset[str] = frozenset(
    {"user", "model", "tool", "system", "goal", "compaction", "repair", "plugin"}
)

MESSAGE_ID_PATTERN = r"^msg_[0-9a-f]{16}$"
MISSING_MESSAGE_ID_PATTERN = r"^msg_missing_[0-9a-f]{16}$"
TOOL_CALL_ID_PATTERN = r"^call_[0-9a-f]{16}$"
MESSAGE_ID_RE = re.compile(MESSAGE_ID_PATTERN)
MISSING_MESSAGE_ID_RE = re.compile(MISSING_MESSAGE_ID_PATTERN)
TOOL_CALL_ID_RE = re.compile(TOOL_CALL_ID_PATTERN)

MISSING_TOOL_RESULT_TEXT = "tool result was never recorded: the turn stopped before the tool finished"

_ROLE_PARTS: dict[str, frozenset[str]] = {
    "system": frozenset({"text"}),
    "developer": frozenset({"text"}),
    "user": frozenset({"text", "image"}),
    "assistant": frozenset({"text", "reasoning", "tool_call"}),
    "tool": frozenset({"text", "image"}),
}


class MessageValidationError(ValueError):
    """A native message does not satisfy the schema 1 contract."""


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str
    type: Literal["text"] = field(default="text", init=False)

    def __post_init__(self) -> None:
        _require_text(self.text, "text part text")


@dataclass(frozen=True, slots=True)
class ReasoningPart:
    text: str
    type: Literal["reasoning"] = field(default="reasoning", init=False)

    def __post_init__(self) -> None:
        _require_text(self.text, "reasoning part text")


@dataclass(frozen=True, slots=True)
class ToolCallPart:
    id: str
    name: str
    arguments: str
    type: Literal["tool_call"] = field(default="tool_call", init=False)

    def __post_init__(self) -> None:
        if not TOOL_CALL_ID_RE.fullmatch(self.id):
            raise MessageValidationError(f"tool call id {self.id!r} must match {TOOL_CALL_ID_PATTERN}")
        _require_text(self.name, "tool call name")
        if not isinstance(self.arguments, str):
            raise MessageValidationError("tool call arguments must be a JSON string")


@dataclass(frozen=True, slots=True)
class ImagePart:
    attachment: str
    media_type: MediaType
    type: Literal["image"] = field(default="image", init=False)

    def __post_init__(self) -> None:
        _require_text(self.attachment, "image attachment")
        if self.media_type not in MEDIA_TYPES:
            raise MessageValidationError(f"image media type {self.media_type!r} is not supported")


type ContentPart = TextPart | ReasoningPart | ToolCallPart | ImagePart
type Part = ContentPart


@dataclass(frozen=True, slots=True)
class UserSource:
    kind: Literal["user"] = field(default="user", init=False)


@dataclass(frozen=True, slots=True)
class ModelSource:
    provider: str
    model: str
    kind: Literal["model"] = field(default="model", init=False)

    def __post_init__(self) -> None:
        _require_text(self.provider, "source provider")
        _require_text(self.model, "source model")


@dataclass(frozen=True, slots=True)
class ToolSource:
    call_id: str
    kind: Literal["tool"] = field(default="tool", init=False)

    def __post_init__(self) -> None:
        if not TOOL_CALL_ID_RE.fullmatch(self.call_id):
            raise MessageValidationError(f"tool source call_id {self.call_id!r} is not a call id")


@dataclass(frozen=True, slots=True)
class SystemSource:
    kind: Literal["system"] = field(default="system", init=False)


@dataclass(frozen=True, slots=True)
class GoalSource:
    goal_id: str
    revision: int
    round: int
    kind: Literal["goal"] = field(default="goal", init=False)

    def __post_init__(self) -> None:
        _require_text(self.goal_id, "goal source goal_id")
        _require_int(self.revision, "goal source revision", minimum=0)
        _require_int(self.round, "goal source round", minimum=0)


@dataclass(frozen=True, slots=True)
class CompactionSource:
    compaction_id: str
    kind: Literal["compaction"] = field(default="compaction", init=False)

    def __post_init__(self) -> None:
        _require_text(self.compaction_id, "compaction source compaction_id")


@dataclass(frozen=True, slots=True)
class RepairSource:
    reason: str
    kind: Literal["repair"] = field(default="repair", init=False)

    def __post_init__(self) -> None:
        _require_text(self.reason, "repair source reason")


@dataclass(frozen=True, slots=True)
class PluginSource:
    plugin: str
    kind: Literal["plugin"] = field(default="plugin", init=False)

    def __post_init__(self) -> None:
        _require_text(self.plugin, "plugin source plugin")


type MessageSource = (
    UserSource
    | ModelSource
    | ToolSource
    | SystemSource
    | GoalSource
    | CompactionSource
    | RepairSource
    | PluginSource
)
type Source = MessageSource


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    role: Role
    content: tuple[ContentPart, ...]
    created_at: int
    source: MessageSource
    tool_call_id: str | None = None
    is_error: bool = False

    def __post_init__(self) -> None:
        if not (MESSAGE_ID_RE.fullmatch(self.id) or MISSING_MESSAGE_ID_RE.fullmatch(self.id)):
            raise MessageValidationError(f"message id {self.id!r} is not a message id")
        if self.role not in ROLES:
            raise MessageValidationError(f"unknown message role {self.role!r}")
        if not isinstance(self.content, tuple) or not self.content:
            raise MessageValidationError("message content must be a non-empty tuple")
        allowed = _ROLE_PARTS[self.role]
        for part in self.content:
            if not isinstance(part, (TextPart, ReasoningPart, ToolCallPart, ImagePart)):
                raise MessageValidationError(f"message contains an unknown part {part!r}")
            if part.type not in allowed:
                raise MessageValidationError(f"role {self.role!r} does not allow a {part.type!r} part")
        _require_int(self.created_at, "message created_at", minimum=0)
        sources = (
            UserSource,
            ModelSource,
            ToolSource,
            SystemSource,
            GoalSource,
            CompactionSource,
            RepairSource,
            PluginSource,
        )
        if not isinstance(self.source, sources):
            raise MessageValidationError("message source is not a native MessageSource")
        if not isinstance(self.is_error, bool):
            raise MessageValidationError("message is_error must be a boolean")

        if self.role == "tool":
            if self.tool_call_id is None:
                raise MessageValidationError("role 'tool' requires tool_call_id")
            if not TOOL_CALL_ID_RE.fullmatch(self.tool_call_id):
                raise MessageValidationError(
                    f"tool message tool_call_id {self.tool_call_id!r} is not a call id"
                )
            if isinstance(self.source, ToolSource):
                if self.source.call_id != self.tool_call_id:
                    raise MessageValidationError("tool message source call_id must equal tool_call_id")
            elif isinstance(self.source, RepairSource):
                if self.source.reason != "missing-tool-result" or not self.is_error:
                    raise MessageValidationError(
                        "role 'tool' repair source must be an is_error missing-tool-result message"
                    )
            else:
                raise MessageValidationError("role 'tool' requires a tool or repair message source")
        else:
            if self.tool_call_id is not None:
                raise MessageValidationError(f"role {self.role!r} must not carry tool_call_id")
            if self.is_error:
                raise MessageValidationError("is_error is only valid on tool messages")

        seen_calls: set[str] = set()
        for part in self.content:
            if isinstance(part, ToolCallPart):
                if part.id in seen_calls:
                    raise MessageValidationError(f"duplicate tool call id {part.id!r} in one message")
                seen_calls.add(part.id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the schema 1 JSON object (null fields omitted)."""
        data: dict[str, Any] = {
            "id": self.id,
            "role": self.role,
            "content": [part_to_dict(part) for part in self.content],
            "created_at": self.created_at,
            "source": source_to_dict(self.source),
        }
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.is_error:
            data["is_error"] = True
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Message:
        """Parse and validate one schema 1 message object."""
        if not isinstance(raw, Mapping):
            raise MessageValidationError("message must be an object")
        unknown = set(raw) - {"id", "role", "content", "created_at", "source", "tool_call_id", "is_error"}
        if unknown:
            name = sorted(unknown)[0]
            raise MessageValidationError(f"message has unknown field {name!r}")
        for required in ("id", "role", "content", "created_at", "source"):
            if required not in raw:
                raise MessageValidationError(f"message lacks required field {required!r}")
        content_raw = raw["content"]
        if not isinstance(content_raw, (list, tuple)):
            raise MessageValidationError("message content must be an array")
        content = tuple(part_from_dict(part) for part in content_raw)
        return cls(
            id=_message_text(raw["id"], "message id"),
            role=_role(raw["role"]),
            content=content,
            created_at=_message_int(raw["created_at"], "message created_at"),
            source=source_from_dict(raw["source"]),
            tool_call_id=_optional_message_text(raw.get("tool_call_id"), "message tool_call_id"),
            is_error=_message_bool(raw.get("is_error", False), "message is_error"),
        )

    def text(self) -> str:
        """Concatenated text parts, in order."""
        return "".join(part.text for part in self.content if isinstance(part, TextPart))

    def tool_calls(self) -> tuple[ToolCallPart, ...]:
        """Tool-call parts in emission order."""
        return tuple(part for part in self.content if isinstance(part, ToolCallPart))


def part_to_dict(part: ContentPart) -> dict[str, Any]:
    """Serialize one content part."""
    if isinstance(part, TextPart):
        return {"type": "text", "text": part.text}
    if isinstance(part, ReasoningPart):
        return {"type": "reasoning", "text": part.text}
    if isinstance(part, ToolCallPart):
        return {"type": "tool_call", "id": part.id, "name": part.name, "arguments": part.arguments}
    if isinstance(part, ImagePart):
        return {"type": "image", "attachment": part.attachment, "media_type": part.media_type}
    raise MessageValidationError(f"unknown content part {part!r}")


def part_from_dict(raw: object) -> ContentPart:
    """Parse one content part object."""
    if not isinstance(raw, Mapping):
        raise MessageValidationError("content part must be an object")
    part_type = raw.get("type")
    if part_type == "text":
        _only_keys(raw, {"type", "text"}, "text part")
        return TextPart(text=_message_text(raw.get("text"), "text part text"))
    if part_type == "reasoning":
        _only_keys(raw, {"type", "text"}, "reasoning part")
        return ReasoningPart(text=_message_text(raw.get("text"), "reasoning part text"))
    if part_type == "tool_call":
        _only_keys(raw, {"type", "id", "name", "arguments"}, "tool call part")
        return ToolCallPart(
            id=_message_text(raw.get("id"), "tool call part id"),
            name=_message_text(raw.get("name"), "tool call part name"),
            arguments=_message_text(raw.get("arguments"), "tool call part arguments"),
        )
    if part_type == "image":
        _only_keys(raw, {"type", "attachment", "media_type"}, "image part")
        media_type = _message_text(raw.get("media_type"), "image part media_type")
        if media_type not in MEDIA_TYPES:
            raise MessageValidationError(f"image media type {media_type!r} is not supported")
        return ImagePart(
            attachment=_message_text(raw.get("attachment"), "image part attachment"),
            media_type=cast("MediaType", media_type),
        )
    raise MessageValidationError(f"unknown content part type {part_type!r}")


def source_to_dict(source: MessageSource) -> dict[str, Any]:
    """Serialize one message source."""
    if isinstance(source, UserSource):
        return {"kind": "user"}
    if isinstance(source, ModelSource):
        return {"kind": "model", "provider": source.provider, "model": source.model}
    if isinstance(source, ToolSource):
        return {"kind": "tool", "call_id": source.call_id}
    if isinstance(source, SystemSource):
        return {"kind": "system"}
    if isinstance(source, GoalSource):
        return {"kind": "goal", "goal_id": source.goal_id, "revision": source.revision, "round": source.round}
    if isinstance(source, CompactionSource):
        return {"kind": "compaction", "compaction_id": source.compaction_id}
    if isinstance(source, RepairSource):
        return {"kind": "repair", "reason": source.reason}
    if isinstance(source, PluginSource):
        return {"kind": "plugin", "plugin": source.plugin}
    raise MessageValidationError(f"unknown message source {source!r}")


def source_from_dict(raw: object) -> MessageSource:
    """Parse one message source object."""
    if not isinstance(raw, Mapping):
        raise MessageValidationError("message source must be an object")
    kind = raw.get("kind")
    if kind == "user":
        _only_keys(raw, {"kind"}, "user source")
        return UserSource()
    if kind == "model":
        _only_keys(raw, {"kind", "provider", "model"}, "model source")
        return ModelSource(
            provider=_message_text(raw.get("provider"), "source provider"),
            model=_message_text(raw.get("model"), "source model"),
        )
    if kind == "tool":
        _only_keys(raw, {"kind", "call_id"}, "tool source")
        return ToolSource(call_id=_message_text(raw.get("call_id"), "source call_id"))
    if kind == "system":
        _only_keys(raw, {"kind"}, "system source")
        return SystemSource()
    if kind == "goal":
        _only_keys(raw, {"kind", "goal_id", "revision", "round"}, "goal source")
        return GoalSource(
            goal_id=_message_text(raw.get("goal_id"), "source goal_id"),
            revision=_message_int(raw.get("revision"), "source revision"),
            round=_message_int(raw.get("round"), "source round"),
        )
    if kind == "compaction":
        _only_keys(raw, {"kind", "compaction_id"}, "compaction source")
        return CompactionSource(compaction_id=_message_text(raw.get("compaction_id"), "source compaction_id"))
    if kind == "repair":
        _only_keys(raw, {"kind", "reason"}, "repair source")
        return RepairSource(reason=_message_text(raw.get("reason"), "source reason"))
    if kind == "plugin":
        _only_keys(raw, {"kind", "plugin"}, "plugin source")
        return PluginSource(plugin=_message_text(raw.get("plugin"), "source plugin"))
    raise MessageValidationError(f"unknown message source kind {kind!r}")


def replace_content(
    message: Message,
    content: tuple[ContentPart, ...],
    *,
    is_error: bool | None = None,
) -> Message:
    """Return a copy with new content/is_error, keeping identity fields intact."""
    return replace(
        message,
        content=content,
        is_error=message.is_error if is_error is None else is_error,
    )


def missing_tool_result_message(call_id: str, created_at: int) -> Message:
    """Derive the deterministic repair message for a call without a result."""
    if not TOOL_CALL_ID_RE.fullmatch(call_id):
        raise MessageValidationError(f"call id {call_id!r} is not a call id")
    return Message(
        id="msg_missing_" + call_id.removeprefix("call_"),
        role="tool",
        content=(TextPart(MISSING_TOOL_RESULT_TEXT),),
        created_at=created_at,
        source=RepairSource("missing-tool-result"),
        tool_call_id=call_id,
        is_error=True,
    )


def message_text(message: Message) -> str:
    """All text parts of message joined in order."""
    return message.text()


def message_tool_calls(message: Message) -> tuple[ToolCallPart, ...]:
    """All tool-call parts of message in order."""
    return message.tool_calls()


def _only_keys(raw: Mapping[str, Any], allowed: set[str], subject: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise MessageValidationError(f"{subject} has unknown field {sorted(unknown)[0]!r}")


def _require_text(value: object, subject: str) -> None:
    if not isinstance(value, str):
        raise MessageValidationError(f"{subject} must be a string")
    if not value:
        raise MessageValidationError(f"{subject} must not be empty")


def _require_int(value: object, subject: str, *, minimum: int = 0) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MessageValidationError(f"{subject} must be an integer")
    if value < minimum:
        raise MessageValidationError(f"{subject} must be at least {minimum}")


def _message_text(value: object, subject: str) -> str:
    _require_text(value, subject)
    assert isinstance(value, str)
    return value


def _message_int(value: object, subject: str) -> int:
    _require_int(value, subject)
    assert isinstance(value, int)
    return value


def _message_bool(value: object, subject: str) -> bool:
    if not isinstance(value, bool):
        raise MessageValidationError(f"{subject} must be a boolean")
    return value


def _optional_message_text(value: object, subject: str) -> str | None:
    if value is None:
        return None
    return _message_text(value, subject)


def _role(value: object) -> Role:
    if not isinstance(value, str) or value not in ROLES:
        raise MessageValidationError(f"unknown message role {value!r}")
    return value  # type: ignore[return-value]


__all__ = [
    "MEDIA_TYPES",
    "MESSAGE_ID_PATTERN",
    "MISSING_MESSAGE_ID_PATTERN",
    "MISSING_TOOL_RESULT_TEXT",
    "PART_TYPES",
    "ROLES",
    "SOURCE_KINDS",
    "TOOL_CALL_ID_PATTERN",
    "CompactionSource",
    "ContentPart",
    "GoalSource",
    "ImagePart",
    "MediaType",
    "Message",
    "MessageSource",
    "MessageValidationError",
    "ModelSource",
    "Part",
    "PartType",
    "PluginSource",
    "ReasoningPart",
    "RepairSource",
    "Role",
    "Source",
    "SourceKind",
    "SystemSource",
    "TextPart",
    "ToolCallPart",
    "ToolSource",
    "UserSource",
    "message_text",
    "message_tool_calls",
    "missing_tool_result_message",
    "part_from_dict",
    "part_to_dict",
    "replace_content",
    "source_from_dict",
    "source_to_dict",
]
