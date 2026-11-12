"""Shared builders and fixture paths for the session test suite."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from deeppyro.core.wire import LogRecord, Message, SessionHeader
from deeppyro.session.events import normalize_event

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "fixtures"
SESSIONS_DIR = FIXTURES / "sessions"
BY_TYPE_DIR = FIXTURES / "records" / "by-type"

requires_fixtures = pytest.mark.skipif(
    not FIXTURES.is_dir(),
    reason="privacy: real-session fixtures are not committed; run node tools/gen_fixtures.mjs",
)


def load_manifest() -> dict[str, Any]:
    """Parsed fixtures/manifest.json."""
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def session_files() -> list[pathlib.Path]:
    """Every real session fixture, sorted by file name."""
    return sorted(SESSIONS_DIR.glob("*.jsonl"))


def by_type_files() -> list[pathlib.Path]:
    """Every per-record-type sample, sorted by file name."""
    return sorted(BY_TYPE_DIR.glob("*.jsonl"))


def manifest_by_file() -> dict[str, dict[str, Any]]:
    """Manifest session entries keyed by fixture file name."""
    return {entry["file"]: entry for entry in load_manifest()["sessions"]}


def header(
    session_id: str = "session-test",
    *,
    cwd: str = "C:/work",
    **overrides: Any,
) -> SessionHeader:
    """A valid v4 header for store/writer tests."""
    payload: dict[str, Any] = {
        "type": "session",
        "version": 4,
        "id": session_id,
        "createdAt": 1_700_000_000_000,
        "cwd": cwd,
        "isSeeded": False,
        "delegationDepth": 0,
    }
    payload.update(overrides)
    return SessionHeader.model_validate(payload)


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def user_message(message_id: str = "msg_user", text: str = "hello") -> dict[str, Any]:
    return {
        "id": message_id,
        "role": "user",
        "content": [text_part(text)],
        "source": {"kind": "user"},
    }


def system_message(message_id: str = "msg_system", text: str = "system prompt") -> dict[str, Any]:
    return {
        "turn": 1,
        "step": 1,
        "message": {
            "id": message_id,
            "role": "system",
            "content": [text_part(text)],
            "source": {"kind": "system-prompt"},
        },
    }


def assistant_message(
    message_id: str = "msg_assistant",
    text: str = "answer",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    turn: int = 1,
    step: int = 1,
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [text_part(text)]
    for call in tool_calls or []:
        content.append({"type": "tool-call", **call})
    return {
        "turn": turn,
        "step": step,
        "message": {
            "id": message_id,
            "role": "assistant",
            "content": content,
            "source": {"kind": "model", "provider": "deepseek", "model": "deepseek-chat"},
        },
        "usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2},
    }


def tool_call(call_id: str = "call_1", name: str = "read", *, turn: int = 1, step: int = 1) -> dict[str, Any]:
    return {"turn": turn, "step": step, "callId": call_id, "name": name, "arguments": "{}"}


def tool_result(
    call_id: str = "call_1",
    text: str = "ok",
    *,
    message_id: str = "msg_tool",
    is_error: bool = False,
    meta: dict[str, Any] | None = None,
    turn: int = 1,
    step: int = 1,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "turn": turn,
        "step": step,
        "message": {
            "id": message_id,
            "role": "tool",
            "toolCallId": call_id,
            "isError": is_error,
            "content": [text_part(text)],
            "source": {"kind": "tool", "callId": call_id},
        },
    }
    if meta is not None:
        data["meta"] = meta
    return data


def developer_message(
    message_id: str = "msg_developer",
    *,
    content: list[dict[str, Any]] | None = None,
    turn: int = 1,
    step: int = 1,
) -> dict[str, Any]:
    return {
        "turn": turn,
        "step": step,
        "message": {
            "id": message_id,
            "role": "developer",
            "content": content if content is not None else [],
            "source": {"kind": "dsh-mnemon", "form": "memory-snapshot-retired"},
        },
    }


def event(
    type: str, seq: int, data: dict[str, Any], *, time: int = 1_700_000_000_000, **envelope: Any
) -> dict[str, Any]:
    """One raw event envelope."""
    payload: dict[str, Any] = {"type": type, "seq": seq, "time": time, "data": data}
    payload.update(envelope)
    return payload


def surface(
    type: str,
    seq: int,
    data: dict[str, Any],
    *,
    op: Any = "append",
    time: int = 1_700_000_000_000,
    **envelope: Any,
) -> dict[str, Any]:
    """A surface event with its required surfaceOp."""
    return event(type, seq, data, time=time, surfaceOp=op, **envelope)


def record(raw: dict[str, Any]) -> LogRecord:
    """Validate one raw event and return its LogRecord."""
    return LogRecord.model_validate(normalize_event(raw))


def message(raw: dict[str, Any]) -> Message:
    return Message.model_validate(raw)
