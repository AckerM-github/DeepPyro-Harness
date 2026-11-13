"""Shared builders for the compress/block/ref test suite."""

from __future__ import annotations

import asyncio
import pathlib
import time
from typing import Any

from deeppyro.compact.blocks import BlockStore
from deeppyro.compact.compress_tool import CompressTool
from deeppyro.compact.refs import RefStore
from deeppyro.core.protocols import ToolContext
from deeppyro.session import Session, SessionStore

LONG_TEXT = (
    "The investigation established the exact failure mode: the driver holds a stale reference "
    "while the device is removed, so every later read returns an error. Reproducing it requires "
    "the verifier enabled and a reboot between attach and detach."
)


def text_part(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def message(
    role: str,
    content: list[dict[str, Any]],
    *,
    message_id: str,
    source: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": message_id, "role": role, "content": content}
    if source is not None:
        payload["source"] = source
    payload.update(extra)
    return payload


def seed_session(
    store: SessionStore,
    *,
    session_id: str = "session-compact-test",
    tool_exchange: bool = True,
) -> Session:
    """Create a small session with a known seq layout.

    Layout (tool_exchange=True):
        seq 0  system/message   system prompt (not addressable)
        seq 1  user/message     m00001
        seq 2  assistant/message m00002
        seq 3  user/message     m00003
        seq 4  assistant/message m00004 (with a tool-call part)
        seq 5  tool/call        (not addressable)
        seq 6  tool/result      m00005
        seq 7  user/message     m00006 (trailing, uncovered by default)
    """
    session = store.create(pathlib.Path("C:/work"), session_id=session_id)
    session.append(
        "system/message",
        {
            "turn": 1,
            "step": 0,
            "message": message(
                "system",
                [text_part("You are a helpful coding agent.")],
                message_id="msg_sys",
                source={"kind": "system-prompt"},
            ),
        },
    )
    session.append(
        "user/message",
        message(
            "user", [text_part("First question. " + LONG_TEXT)], message_id="msg_u1", source={"kind": "user"}
        ),
    )
    session.append(
        "assistant/message",
        {
            "turn": 1,
            "step": 0,
            "usage": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
            "stream": [],
            "message": message(
                "assistant",
                [text_part("First answer. " + LONG_TEXT)],
                message_id="msg_a1",
                source={"kind": "model", "provider": "test", "model": "test-model"},
            ),
        },
    )
    session.append(
        "user/message",
        message(
            "user", [text_part("Second question. " + LONG_TEXT)], message_id="msg_u2", source={"kind": "user"}
        ),
    )
    assistant_parts: list[dict[str, Any]] = [text_part("Calling a tool. " + LONG_TEXT)]
    if tool_exchange:
        assistant_parts.append({"type": "tool-call", "id": "call_x", "name": "read", "arguments": "{}"})
    session.append(
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
            "stream": [],
            "message": message(
                "assistant",
                assistant_parts,
                message_id="msg_a2",
                source={"kind": "model", "provider": "test", "model": "test-model"},
            ),
        },
    )
    if tool_exchange:
        session.append(
            "tool/call", {"turn": 1, "step": 1, "callId": "call_x", "name": "read", "arguments": "{}"}
        )
        session.append(
            "tool/result",
            {
                "turn": 1,
                "step": 1,
                "message": message(
                    "tool",
                    [text_part("tool output " + LONG_TEXT)],
                    message_id="msg_t1",
                    source={"kind": "tool", "callId": "call_x"},
                    toolCallId="call_x",
                    isError=False,
                ),
            },
        )
    session.append(
        "user/message",
        message(
            "user",
            [text_part("Trailing question. " + LONG_TEXT)],
            message_id="msg_u3",
            source={"kind": "user"},
        ),
    )
    return session


def stores(tmp_path: pathlib.Path) -> tuple[SessionStore, pathlib.Path, BlockStore, RefStore]:
    """Create the session store plus block/ref stores rooted under tmp_path."""
    root = tmp_path / "sessions"
    block_root = root / "blocks"
    return SessionStore(root=root), root, BlockStore(block_root), RefStore(block_root)


def make_tool(tmp_path: pathlib.Path) -> tuple[CompressTool, SessionStore, BlockStore, RefStore]:
    session_store, _root, blocks, refs = stores(tmp_path)
    return CompressTool(session_store, blocks, refs), session_store, blocks, refs


def make_ctx(
    session: Session | None = None,
    *,
    session_id: str | None = None,
    services: dict[str, Any] | None = None,
    turn: int = 2,
    step: int = 0,
) -> ToolContext:
    payload: dict[str, Any] = {"session": session} if session is not None else {}
    if services:
        payload.update(services)
    return ToolContext(
        cwd=pathlib.Path("C:/work"),
        session_id=session_id if session_id is not None else (session.session_id if session else "missing"),
        turn=turn,
        step=step,
        call_id="call_compress",
        cancel=asyncio.Event(),
        log=lambda *_args: None,
        services=payload,
    )


def local_stamp(milliseconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(milliseconds / 1000))


def checkpoint_messages(session: Session) -> list[Any]:
    return [
        item for item in session.derive_messages() if (item.source or {}).get("kind") == "compact-checkpoint"
    ]


__all__ = [
    "LONG_TEXT",
    "checkpoint_messages",
    "local_stamp",
    "make_ctx",
    "make_tool",
    "message",
    "seed_session",
    "stores",
    "text_part",
]
