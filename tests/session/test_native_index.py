"""Read-only schema 1 (native) session index for the TUI session browser."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.core.messages import (
    Message,
    ModelSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.session.native_index import (
    NativeSessionInfo,
    list_native_sessions,
    read_native_transcript,
)
from deeppyro.session.schema import EventKind, NativeSession

CWD = "C:/work/deeppyro"
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


def _user_message(text: str, created_at: int) -> Message:
    return Message(
        id=USER_ID,
        role="user",
        content=(TextPart(text),),
        created_at=created_at,
        source=UserSource(),
    )


def _assistant_message(text: str, created_at: int, *, with_tool: bool) -> Message:
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


def _tool_message(text: str, created_at: int) -> Message:
    return Message(
        id=TOOL_ID,
        role="tool",
        content=(TextPart(text),),
        created_at=created_at,
        source=ToolSource(call_id=CALL_ID),
        tool_call_id=CALL_ID,
    )


def _write_native(
    directory: Path,
    *,
    session_id: str = SESSION_ID,
    created_at: int = CREATED_AT,
    title: str | None = TITLE,
    with_tool: bool = True,
    dropped_result: bool = False,
    user_text: str = "Please read the file.",
    assistant_text: str = "I read the file.",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    stamp = created_at
    with NativeSession.create(path, cwd=CWD, created_at=created_at) as session:

        def append(kind: EventKind, data: dict[str, object] | None = None, **assoc: str) -> None:
            nonlocal stamp
            stamp += 1
            session.append(kind, data, ts=stamp, **assoc)

        append(EventKind.TURN_STARTED, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
        user = _user_message(user_text, stamp)
        append(EventKind.USER_MESSAGE, {"message": user.to_dict()}, turn_id=TURN_ID)
        append(
            EventKind.STEP_STARTED,
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
        )
        assistant = _assistant_message(assistant_text, stamp, with_tool=with_tool)
        append(
            EventKind.ASSISTANT_MESSAGE,
            {"message": assistant.to_dict(), "finish_reason": "tool_calls" if with_tool else "stop"},
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
                tool = _tool_message("file contents", stamp)
                append(
                    EventKind.TOOL_RESULT,
                    {"call_id": CALL_ID, "message": tool.to_dict()},
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
        append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
        )
    return path


def _info(directory: Path, session_id: str = SESSION_ID) -> NativeSessionInfo:
    matches = [info for info in list_native_sessions(directory) if info.id == session_id]
    assert len(matches) == 1
    return matches[0]


def test_happy_path_lists_title_preview_turns_and_counts(tmp_path: Path) -> None:
    path = _write_native(tmp_path)

    infos = list_native_sessions(tmp_path)

    assert len(infos) == 1
    info = infos[0]
    assert isinstance(info, NativeSessionInfo)
    assert info.id == SESSION_ID
    assert info.path == path
    assert info.cwd == str(Path(CWD).resolve())
    assert info.created_at == CREATED_AT
    assert info.updated_at == CREATED_AT + 9
    assert info.title == TITLE
    assert info.turns == 1
    assert info.event_count == 9
    assert info.preset == "standard"
    assert info.parent_id is None
    assert info.origin == "user"
    assert info.preview == "Please read the file."
    assert info.source == "native"
    assert info.readable is True
    assert info.read_only is True


def test_listing_is_newest_first(tmp_path: Path) -> None:
    older_id = "session-00000000-0000-0000-0000-000000000001"
    newer_id = "session-00000000-0000-0000-0000-000000000002"
    _write_native(tmp_path, session_id=older_id, created_at=CREATED_AT)
    _write_native(tmp_path, session_id=newer_id, created_at=CREATED_AT + 10_000)

    ids = [info.id for info in list_native_sessions(tmp_path)]

    assert ids == [newer_id, older_id]


def test_mixed_directory_skips_v4_foreign_and_malformed_files(tmp_path: Path) -> None:
    nested_id = "session-00000000-0000-0000-0000-000000000003"
    _write_native(tmp_path)
    _write_native(tmp_path / "project", session_id=nested_id)
    (tmp_path / "session-v4.jsonl").write_text(json.dumps({"version": 4}) + "\n", encoding="utf-8")
    (tmp_path / "other.jsonl").write_text(
        json.dumps({"type": "session", "schema": 2}) + "\n", encoding="utf-8"
    )
    (tmp_path / "broken.jsonl").write_text("not json\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored\n", encoding="utf-8")

    infos = list_native_sessions(tmp_path)

    assert sorted(info.id for info in infos) == sorted([SESSION_ID, nested_id])


def test_missing_or_empty_root_is_empty(tmp_path: Path) -> None:
    assert list_native_sessions(tmp_path / "does-not-exist") == []
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list_native_sessions(empty) == []


def test_unreadable_fold_still_lists_the_session(tmp_path: Path) -> None:
    _write_native(tmp_path, title=None, dropped_result=True)

    info = _info(tmp_path)

    assert info.readable is False
    assert info.event_count == 7
    assert info.turns == 1
    assert info.title == "Please read the file"
    assert info.preview == "Please read the file."
    transcript = read_native_transcript(SESSION_ID, root=tmp_path)
    assert transcript.readable is False
    assert transcript.entries == ()
    assert transcript.issues
    assert "ToolPairingError" in transcript.issues[0]


def test_transcript_has_ordered_messages_and_tool_names(tmp_path: Path) -> None:
    _write_native(tmp_path)

    transcript = read_native_transcript(SESSION_ID, root=tmp_path)

    assert transcript.readable is True
    assert transcript.header.id == SESSION_ID
    assert transcript.info.title == TITLE
    assert [entry.role for entry in transcript.entries] == ["user", "assistant", "tool"]
    user, assistant, tool = transcript.entries
    assert user.text == "Please read the file."
    assert user.tool_names == ()
    assert assistant.text == "I read the file."
    assert assistant.tool_names == ("read_file",)
    assert tool.text == "file contents"
    assert tool.tool_names == ("read_file",)
    assert tool.is_error is False
    assert transcript.final_text == "I read the file."


def test_transcript_is_found_in_a_nested_layout(tmp_path: Path) -> None:
    nested_id = "session-00000000-0000-0000-0000-000000000004"
    _write_native(tmp_path / "project" / "sessions", session_id=nested_id)

    transcript = read_native_transcript(nested_id, root=tmp_path)

    assert transcript.header.id == nested_id
    assert transcript.entries[0].text == "Please read the file."


def test_unknown_transcript_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_native_transcript("session-00000000-0000-0000-0000-0000000000ff", root=tmp_path)


def test_title_and_preview_are_bounded_when_derived(tmp_path: Path) -> None:
    _write_native(tmp_path, title=None, with_tool=False, user_text="x" * 100)

    info = _info(tmp_path)

    assert info.title == "x" * 23 + "…"
    assert info.preview == "x" * 79 + "…"
