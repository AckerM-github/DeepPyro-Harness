"""Shared builders for native (schema 1) session tests.

This module is not collected by pytest; it only provides a compact session fixture
that the native query and fork tests share.
"""

from __future__ import annotations

from pathlib import Path

from deeppyro.core.messages import (
    Message,
    ModelSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.session.schema import EventKind, NativeSession

CWD = "C:/work/deeppyro"
OTHER_CWD = "C:/work/elsewhere"
RUN_ID = "run_" + "1" * 16
TURN_ID = "turn_" + "2" * 16
STEP_ID = "step_" + "3" * 16
TRACE_ID = "a" * 32
CALL_ID = "call_" + "4" * 16
USER_ID = "msg_" + "5" * 16
ASSISTANT_ID = "msg_" + "6" * 16
TOOL_ID = "msg_" + "7" * 16
SESSION_ID = "session-12345678-1234-1234-1234-123456789abc"
CREATED_AT = 1_700_000_000_000
TITLE = "Fix the parser"
ARCHIVE_EVENT = "plugin.session.archived"


def user_message(text: str, created_at: int) -> Message:
    return Message(
        id=USER_ID,
        role="user",
        content=(TextPart(text),),
        created_at=created_at,
        source=UserSource(),
    )


def assistant_message(text: str, created_at: int, *, with_tool: bool) -> Message:
    content: tuple[TextPart | ToolCallPart, ...] = (TextPart(text),)
    if with_tool:
        content = (*content, ToolCallPart(id=CALL_ID, name="read_file", arguments="{}"))
    return Message(
        id=ASSISTANT_ID,
        role="assistant",
        content=content,
        created_at=created_at,
        source=ModelSource(provider="fake", model="fake-1"),
    )


def tool_message(text: str, created_at: int) -> Message:
    return Message(
        id=TOOL_ID,
        role="tool",
        content=(TextPart(text),),
        created_at=created_at,
        source=ToolSource(call_id=CALL_ID),
        tool_call_id=CALL_ID,
    )


def write_native(
    directory: Path,
    *,
    session_id: str = SESSION_ID,
    created_at: int = CREATED_AT,
    title: str | None = TITLE,
    with_tool: bool = True,
    dropped_result: bool = False,
    user_text: str = "Please read the file.",
    assistant_text: str = "I read the file.",
    cwd: str = CWD,
    archived: bool | None = None,
) -> Path:
    """Write one valid native session log and return its path."""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    stamp = created_at
    with NativeSession.create(path, cwd=cwd, created_at=created_at) as session:

        def append(kind: EventKind | str, data: dict[str, object] | None = None, **assoc: str) -> None:
            nonlocal stamp
            stamp += 1
            session.append(kind, data, ts=stamp, **assoc)

        append(EventKind.TURN_STARTED, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
        append(EventKind.USER_MESSAGE, {"message": user_message(user_text, stamp).to_dict()}, turn_id=TURN_ID)
        append(
            EventKind.STEP_STARTED,
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
        )
        append(
            EventKind.ASSISTANT_MESSAGE,
            {
                "message": assistant_message(assistant_text, stamp, with_tool=with_tool).to_dict(),
                "finish_reason": "tool_calls" if with_tool else "stop",
            },
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
        )
        if with_tool:
            append(
                EventKind.TOOL_CALL,
                {"call_id": CALL_ID, "name": "read_file", "arguments": "{}"},
                run_id=RUN_ID,
                turn_id=TURN_ID,
                step_id=STEP_ID,
                trace_id=TRACE_ID,
            )
            if not dropped_result:
                append(
                    EventKind.TOOL_RESULT,
                    {"call_id": CALL_ID, "message": tool_message("file contents", stamp).to_dict()},
                    run_id=RUN_ID,
                    turn_id=TURN_ID,
                    step_id=STEP_ID,
                    trace_id=TRACE_ID,
                )
        append(
            EventKind.STEP_ENDED,
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
        )
        if title is not None:
            append(EventKind.SESSION_TITLE, {"title": title, "source": "user"})
        if archived is not None:
            append(ARCHIVE_EVENT, {"archived": archived, "source": "user", "at": created_at + 20})
        append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
        )
    return path
