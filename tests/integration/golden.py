"""Replay helpers: turn real recorded sessions into scripted provider responses.

The durable log records every assistant message as wire content (text, reasoning,
tool-call parts). Feeding those back through a ScriptedProvider lets integration
tests run the loop against real model behaviour without network access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import zstandard

from tests.integration.fake_provider import Response


def _decompress(path: Path) -> bytes:
    raw = path.read_bytes()
    if path.suffix == ".zstd":
        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=1 << 31)
    return raw


def load_session_records(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read header plus records from a session.v4 log (plain or zstd)."""
    lines = _decompress(Path(path)).decode("utf-8").splitlines()
    header = json.loads(lines[0])
    records = [json.loads(line) for line in lines[1:] if line.strip()]
    return header, records


def assistant_responses(records: list[dict[str, Any]]) -> list[Response]:
    """Project recorded assistant/message events to scripted responses."""
    responses: list[Response] = []
    for record in records:
        if record.get("type") != "assistant/message":
            continue
        data = record.get("data") or {}
        if data.get("interrupted"):
            continue
        message = data.get("message") or {}
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: list[tuple[str, str, str]] = []
        for part in message.get("content") or []:
            kind = part.get("type")
            if kind == "text":
                text_parts.append(part.get("text") or "")
            elif kind == "reasoning":
                reasoning_parts.append(part.get("text") or "")
            elif kind == "tool-call":
                calls.append(
                    (
                        str(part.get("id") or ""),
                        str(part.get("name") or ""),
                        str(part.get("arguments") or ""),
                    )
                )
        responses.append(
            Response(
                text="".join(text_parts),
                reasoning="".join(reasoning_parts),
                tool_calls=calls,
                finish="tool-calls" if calls else "stop",
            )
        )
    return responses


def user_texts(records: list[dict[str, Any]]) -> list[str]:
    """Text of every user/message event, in log order."""
    texts: list[str] = []
    for record in records:
        if record.get("type") != "user/message":
            continue
        data = record.get("data") or {}
        content = data.get("content") or []
        parts = [part.get("text") for part in content if part.get("type") == "text"]
        texts.append("".join(part for part in parts if part))
    return texts


def first_session_with_assistant(session_dir: str | Path) -> Path:
    """Pick the smallest fixture session that has at least one assistant reply."""
    candidates = sorted(Path(session_dir).glob("*.jsonl"), key=lambda p: p.stat().st_size)
    for path in candidates:
        _, records = load_session_records(path)
        if assistant_responses(records):
            return path
    raise AssertionError("no fixture session with assistant messages")
