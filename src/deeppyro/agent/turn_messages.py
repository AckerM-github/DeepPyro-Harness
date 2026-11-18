"""Native turn message conversion: provider responses and tool results to schema messages.

One home for the conversions the native loop, the compaction writer and the
native agent factory all need. Message construction used to live inside
agent/loop.py; keeping it here means a model response or a tool result is
represented exactly once, in the same shape, no matter which writer persists it.

The module is deliberately wire-free (until the tool contract moves): it imports
only deeppyro.core.messages, deeppyro.core.llm, deeppyro.core.ids and the
ToolResult/Tool dataclasses. It never imports core.wire, compact/, tui/ or the
v4 runtime.

Every helper is bounded and deterministic: payloads are copied field by field,
unknown wire fields are ignored, an unresolvable image part is rejected with a
ValueError instead of being silently dropped, and timestamps only default to
now_ms() when the caller does not pass created_at.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any, cast

from deeppyro.agent.errors import ProviderStreamError
from deeppyro.core.ids import new_message_id, now_ms
from deeppyro.core.llm import ModelResponse, ToolSpec
from deeppyro.core.messages import (
    MEDIA_TYPES,
    ContentPart,
    ImagePart,
    MediaType,
    Message,
    MessageSource,
    ModelSource,
    PluginSource,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.core.protocols import Tool, ToolResult

__all__ = [
    "assistant_message",
    "image_part_from_wire",
    "notice_message",
    "tool_message",
    "tool_spec",
    "tool_specs",
    "user_message",
]

_DEFAULT_MEDIA_TYPE = "image/png"
"""Used when a wire image part does not name a supported media type."""


def user_message(
    text: str,
    *,
    source: MessageSource | None = None,
    created_at: int | None = None,
) -> Message:
    """Build the native user message for one prompt.

    created_at defaults to now_ms(); the loop passes the session-ordered
    timestamp it computed so a turn's messages stay monotonic.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("user message text must be a non-empty string")
    return Message(
        id=new_message_id(),
        role="user",
        content=(TextPart(text=text),),
        created_at=now_ms() if created_at is None else created_at,
        source=UserSource() if source is None else source,
    )


def assistant_message(
    response: ModelResponse,
    *,
    provider: str = "",
    model: str = "",
    created_at: int | None = None,
) -> Message:
    """Map one native model response to the persisted assistant message.

    Reasoning and text parts are kept in that order and every tool call becomes
    a ToolCallPart in the order the provider streamed it. An empty response is
    a provider fault, not an empty message: raise ProviderStreamError like the
    loop always did.

    ModelResponse carries no provider/model, so the caller names the call it
    made; the loop passes the same values it put on the ModelRequest.
    """
    parts: list[ContentPart] = []
    if response.reasoning:
        parts.append(ReasoningPart(text=response.reasoning))
    if response.text:
        parts.append(TextPart(text=response.text))
    for call in response.tool_calls:
        parts.append(ToolCallPart(id=call.call_id, name=call.name, arguments=call.arguments))
    if not parts:
        raise ProviderStreamError("provider returned an empty assistant message")
    return Message(
        id=new_message_id(),
        role="assistant",
        content=tuple(parts),
        created_at=now_ms() if created_at is None else created_at,
        source=ModelSource(provider=provider or "unknown", model=model or "unknown"),
    )


def tool_message(
    call_id: str,
    result: ToolResult,
    *,
    created_at: int | None = None,
) -> Message:
    """Map one tool outcome to the persisted tool message.

    result.text or " " is the required text part (an empty tool message is not
    a legal native message); image entries in result.parts become native
    ImagePart objects so the session and the provider agree on the reference.
    """
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("tool message requires a non-empty call id")
    text = result.text if result.text else " "
    parts: list[ContentPart] = [TextPart(text=text)]
    for raw in result.parts:
        image = _image_part_or_none(raw)
        if image is not None:
            parts.append(image)
    return Message(
        id=new_message_id(),
        role="tool",
        content=tuple(parts),
        created_at=now_ms() if created_at is None else created_at,
        source=ToolSource(call_id=call_id),
        tool_call_id=call_id,
        is_error=bool(result.is_error),
    )


def image_part_from_wire(part: Any) -> ImagePart:
    """Convert one wire image part to a native ImagePart.

    Mirrors deeppyro.llm.attachments.image_part_url so the session keeps the
    same reference the provider would resolve: accepts {"type": "image", ...}
    and {"attachment": ..., "media_type": ...}; a string attachment is stored
    as-is, a reference dict contributes its id/url/attachment, and the media
    type comes from media_type/mediaType or a data: URL prefix.
    """
    raw = _mapping(part)
    if raw is None:
        raise ValueError(f"image part must be a mapping, got {type(part).__name__}")
    raw_type = raw.get("type")
    if raw_type is not None and raw_type != "image":
        raise ValueError(f"not an image part: type {raw_type!r}")
    attachment = _attachment_reference(raw)
    if not attachment:
        raise ValueError("image part has no attachment reference")
    media_type = _media_type(raw.get("media_type") or raw.get("mediaType"), attachment)
    return ImagePart(attachment=attachment, media_type=cast("MediaType", media_type))


def tool_spec(tool: Tool) -> ToolSpec:
    """Build the model-facing ToolSpec for one registered native tool."""
    parameters = copy.deepcopy(dict(tool.parameters))
    output_raw = getattr(tool, "output_schema", None)
    output = copy.deepcopy(dict(output_raw)) if isinstance(output_raw, Mapping) else None
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        parameters=parameters,
        output=output,
    )


def tool_specs(tools: Iterable[Tool]) -> tuple[ToolSpec, ...]:
    """Convert an iterable of native tools to specs in registration order."""
    return tuple(tool_spec(tool) for tool in tools)


def notice_message(text: str, *, plugin: str, form: str | None = None) -> Message:
    """Build the native user message for an out-of-band notice.

    Notices (working-directory changes, schedule prompts, subagent settlement)
    are stored as user messages whose source names the plugin, so the fold keeps
    their origin. form is validated for compatibility with v4 notice sources;
    native messages carry only the plugin label.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("notice text must be a non-empty string")
    if not isinstance(plugin, str) or not plugin:
        raise ValueError("notice plugin must be a non-empty string")
    if form is not None and (not isinstance(form, str) or not form):
        raise ValueError("notice form must be a non-empty string when given")
    return Message(
        id=new_message_id(),
        role="user",
        content=(TextPart(text=text),),
        created_at=now_ms(),
        source=PluginSource(plugin=plugin),
    )


def _mapping(part: Any) -> Mapping[str, Any] | None:
    if isinstance(part, Mapping):
        return part
    as_dict = getattr(part, "to_dict", None)
    if callable(as_dict):
        value = as_dict()
        return value if isinstance(value, Mapping) else None
    return None


def _attachment_reference(raw: Mapping[str, Any]) -> str:
    attachment = raw.get("attachment")
    if isinstance(attachment, str):
        return attachment
    if isinstance(attachment, Mapping):
        for key in ("id", "attachment", "url", "path"):
            candidate = attachment.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        return ""
    if attachment is None:
        for key in ("id", "url"):
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


def _media_type(explicit: Any, attachment: str) -> str:
    if isinstance(explicit, str) and explicit:
        if explicit not in MEDIA_TYPES:
            raise ValueError(f"unsupported image media type {explicit!r}")
        return explicit
    from_data_url = _data_url_media_type(attachment)
    if from_data_url is not None:
        return from_data_url
    return _DEFAULT_MEDIA_TYPE


def _data_url_media_type(value: str) -> str | None:
    if not value.startswith("data:"):
        return None
    head = value[: value.find(";")] if ";" in value else value
    media_type = head[len("data:") :]
    return media_type if media_type in MEDIA_TYPES else None


def _image_part_or_none(part: Any) -> ImagePart | None:
    raw = _mapping(part)
    if raw is None:
        return None
    raw_type = raw.get("type")
    if raw_type is not None and raw_type != "image":
        return None
    if raw_type is None and "attachment" not in raw:
        return None
    return image_part_from_wire(raw)
