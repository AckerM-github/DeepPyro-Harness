"""Conversion from core wire messages to the Anthropic Messages API payload.

The Anthropic wire format differs from OpenAI chat-completions in ways that matter for
replays (docs/spec/llm.md section 14):

* instructions are a single top-level `system` string, not a message;
* assistant content is a list of blocks: `text`, `thinking` (reasoning) and `tool_use`;
* tool results are `tool_result` blocks inside `user` messages, not a `tool` role, and
  Anthropic requires a strict call/result pairing;
* tool_use `input` is a JSON *object* (unlike OpenAI, which keeps arguments a string).

P1 scope (Lead decisions, docs/plan/p1-providers.md section 2.1.5):

* `reasoning_effort="off"` (the default) drops history reasoning, because a thinking
  block is only replayable with its signature and core wire/Finish have no channel for it.
  When thinking is enabled the block is sent without a signature; single-turn works,
  multi-turn tool loops may be rejected (documented risk R1).
* No in-history system snapshots, no Files API and no native image URLs: only inline
  base64 images are converted, and bare attachment ids are skipped.
* Tool-call argument JSON that does not parse to an object becomes `{}` (lenient, matching
  DSH) instead of failing the whole turn.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from deeppyro.core.protocols import LlmRequest, ToolSchema
from deeppyro.core.wire import Message, ToolCallPart
from deeppyro.llm.errors import CODE_BAD_REQUEST, LlmRequestError
from deeppyro.llm.messages import (
    _part_text,
    _part_type,
    image_part_url,
    message_text,
)

#: Anthropic accepts these effort levels; "off" disables thinking entirely.
REASONING_EFFORTS = ("off", "low", "medium", "high", "max")

_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def tool_to_anthropic(tool: ToolSchema) -> dict[str, Any]:
    """Convert a ToolSchema to an Anthropic tool definition."""
    parameters = tool.parameters if isinstance(tool.parameters, dict) and tool.parameters else None
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": parameters or dict(_DEFAULT_INPUT_SCHEMA),
    }


def convert_anthropic_messages(
    messages: Sequence[Message],
    system: str | None,
    *,
    reasoning_effort: str = "off",
    in_history_system: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    """Convert core messages to (anthropic messages, top-level system string).

    Adjacent messages with the same role are merged, as Anthropic requires strict
    user/assistant alternation (tool results are user messages). `in_history_system` is
    accepted for signature compatibility but ignored in P1: every system/developer message
    is folded into the top-level system string instead of being replayed in history.
    """
    system_parts: list[str] = []
    if system is not None and system.strip():
        system_parts.append(system)
    converted: list[dict[str, Any]] = []
    pending: set[str] = set()
    seen_calls: set[str] = set()

    def unresolved() -> LlmRequestError:
        ids = ", ".join(sorted(pending))
        return LlmRequestError(
            f"tool calls without a matching tool result: {ids}",
            code=CODE_BAD_REQUEST,
        )

    for message in messages:
        role = message.role
        if role in ("system", "developer"):
            text = message_text(message)
            if text.strip():
                system_parts.append(text)
            continue
        if role == "assistant":
            if pending:
                raise unresolved()
            blocks: list[dict[str, Any]] = []
            for part in message.content:
                part_type = _part_type(part)
                if part_type == "text":
                    text = _part_text(part)
                    if text:
                        blocks.append({"type": "text", "text": text})
                elif part_type == "reasoning":
                    text = _part_text(part)
                    if reasoning_effort != "off" and text:
                        # No signature in P1: see the module docstring / risk R1.
                        blocks.append({"type": "thinking", "thinking": text})
                elif part_type == "tool-call":
                    if isinstance(part, ToolCallPart):
                        call = part
                    else:
                        call = ToolCallPart.model_validate(part)
                    if call.id in seen_calls:
                        raise LlmRequestError(
                            f"duplicate tool_use id in conversation: {call.id!r}",
                            code=CODE_BAD_REQUEST,
                        )
                    seen_calls.add(call.id)
                    pending.add(call.id)
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": _tool_input(call.arguments),
                        }
                    )
            if blocks:
                _append_blocks(converted, "assistant", blocks)
            continue
        if role == "tool":
            call_id = message.toolCallId
            if not call_id and isinstance(message.source, dict):
                raw = message.source.get("callId")
                call_id = raw if isinstance(raw, str) else None
            if not call_id or call_id not in pending:
                raise LlmRequestError(
                    f"tool result without a matching tool call: {call_id!r}",
                    code=CODE_BAD_REQUEST,
                )
            pending.discard(call_id)
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call_id,
                "content": [{"type": "text", "text": message_text(message)}],
            }
            if message.isError:
                block["is_error"] = True
            _append_blocks(converted, "user", [block])
            continue
        if role == "user":
            if pending:
                raise unresolved()
            blocks = []
            for part in message.content:
                part_type = _part_type(part)
                if part_type == "text":
                    text = _part_text(part)
                    if text:
                        blocks.append({"type": "text", "text": text})
                elif part_type == "image":
                    block = _image_block(part)
                    if block is not None:
                        blocks.append(block)
            if blocks:
                _append_blocks(converted, "user", blocks)
            continue
        # Unknown roles are ignored (the wire enum already constrains this).

    if pending:
        raise unresolved()
    if not converted:
        raise LlmRequestError(
            "conversation has no messages to send",
            code=CODE_BAD_REQUEST,
        )
    system_text = "\n\n".join(part for part in system_parts if part.strip()) or None
    return converted, system_text


def build_anthropic_payload(
    req: LlmRequest,
    *,
    default_max_tokens: int = _DEFAULT_MAX_TOKENS,
    reasoning_effort: str = "off",
    model_max_tokens: int | None = None,
    in_history_system: bool = False,
) -> dict[str, Any]:
    """Build the body for POST /v1/messages.

    Conversion errors are raised before any HTTP request is made, so they never consume
    a retry attempt. `max_tokens` is mandatory for Anthropic and follows
    request -> model capability -> default. The context window is not pre-checked: the
    server answers with a context-window error that is classified accordingly.
    """
    if reasoning_effort not in REASONING_EFFORTS:
        raise LlmRequestError(
            f"unsupported reasoning effort {reasoning_effort!r}; expected one of {REASONING_EFFORTS}",
            code=CODE_BAD_REQUEST,
        )
    messages, system = convert_anthropic_messages(
        req.messages,
        req.system,
        reasoning_effort=reasoning_effort,
        in_history_system=in_history_system,
    )
    max_tokens = req.max_tokens or model_max_tokens or default_max_tokens
    if not isinstance(max_tokens, int) or max_tokens <= 0:
        max_tokens = _DEFAULT_MAX_TOKENS
    payload: dict[str, Any] = {
        "model": req.model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if req.temperature is not None:
        payload["temperature"] = req.temperature
    tools = [tool_to_anthropic(tool) for tool in req.tools]
    if tools:
        payload["tools"] = tools
    if reasoning_effort == "off":
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled"}
        payload["output_config"] = {"effort": reasoning_effort}
    return payload


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _append_blocks(
    converted: list[dict[str, Any]],
    role: str,
    blocks: list[dict[str, Any]],
) -> None:
    """Append blocks, merging into the previous message when the role repeats."""
    if converted and converted[-1]["role"] == role:
        converted[-1]["content"].extend(blocks)
    else:
        converted.append({"role": role, "content": list(blocks)})


def _tool_input(arguments: str | None) -> dict[str, Any]:
    """Parse tool-call arguments into the object Anthropic requires; {} on failure."""
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _image_block(part: Any) -> dict[str, Any] | None:
    """Convert an ImagePart to Anthropic's inline base64 image block.

    Only data: URLs are supported; http(s) URLs and bare attachment ids are skipped
    (there is no Files API or URL fetch in P1).
    """
    url = image_part_url(part)
    if not url or not url.startswith("data:"):
        return None
    header, _, data = url.partition(",")
    if not data:
        return None
    media_type = header[len("data:") :].split(";", 1)[0] or "image/png"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


__all__ = [
    "REASONING_EFFORTS",
    "build_anthropic_payload",
    "convert_anthropic_messages",
    "tool_to_anthropic",
]
