"""Differential replay: the native loop against the frozen recordings.

Each case in `fixtures/native/recordings/` is replayed through the real
:class:`AgentLoop` (with recorded tool results) and the resulting native session
events must match the recorded assistant messages, tool calls and tool results.
Recordings are never modified: the replay provider and the recorded tools are
test-only adapters built from the fixture bytes.
"""

from __future__ import annotations

import json

import pytest

from deeppyro.agent.loop import AgentLoop
from deeppyro.core.ids import new_session_id
from deeppyro.core.messages import TOOL_CALL_ID_RE, Message
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeCodec, NativeSession
from tests.native.replay import (
    Recording,
    ReplayProvider,
    load_recording,
    recording_paths,
    recording_tools,
)

CASES = recording_paths()
CASE_IDS = [case_id for case_id, _ in CASES]


def _messages(events: list[object], kind: EventKind) -> list[Message]:
    out: list[Message] = []
    for event in events:
        if getattr(event, "type", None) != kind.value:
            continue
        data = getattr(event, "data", {})
        out.append(Message.from_dict(data["message"]))
    return out


async def _drive(recording: Recording, tmp_path) -> tuple[object, ReplayProvider, list[object]]:
    session_path = tmp_path / "sessions" / f"{new_session_id()}.jsonl"
    session = NativeSession.create(session_path, cwd=tmp_path, session_id=session_path.stem)
    provider = ReplayProvider(recording.exchanges)
    tools = recording_tools(recording)
    loop = AgentLoop(
        session=session,
        provider=provider,
        model="deepseek-flash",
        tools=tools,
        cwd=tmp_path,
    )
    results = []
    try:
        for prompt in recording.prompts:
            results.append(await loop.run(prompt))
    finally:
        session.close()
    log = NativeCodec.read(session_path)
    return (log, provider, results)


async def test_recorded_prompt_split_is_understood() -> None:
    for case_id, path in CASES:
        recording = load_recording(path)
        if case_id == "09_tool_sequential":
            assert recording.prompts == (
                "Use the list_dir tool to list the directory D:/dshworkspace/m0/data. "
                "In this same response call only list_dir; do not call read_file yet.",
            )
            assert recording.injected_prompts == (
                "Now use the read_file tool to read the file hello.txt in that directory.",
            )
        else:
            assert recording.injected_prompts == (), case_id


@pytest.mark.parametrize(("case_id", "path"), CASES, ids=CASE_IDS)
async def test_native_loop_matches_recording(case_id: str, path, tmp_path) -> None:
    recording = load_recording(path)
    log, provider, results = await _drive(recording, tmp_path)
    events = list(log.events)
    view = fold(log.header, events)

    assert view.issues == ()
    assert provider.consumed == len(recording.exchanges)
    assert len(provider.requests) == len(recording.exchanges)
    assert [result.status.value for result in results] == ["completed"] * len(recording.prompts)
    assert results[-1].final_text == recording.exchanges[-1].content

    users = [message.text() for message in _messages(events, EventKind.USER_MESSAGE)]
    assert users == list(recording.prompts)

    assistant_messages = _messages(events, EventKind.ASSISTANT_MESSAGE)
    assistant_finishes = [
        event.data["finish_reason"] for event in events if event.type == EventKind.ASSISTANT_MESSAGE.value
    ]
    assert [
        (message.text(), finish)
        for message, finish in zip(assistant_messages, assistant_finishes, strict=False)
    ] == list(recording.assistant_expected)

    tool_calls = [
        (event.data["call_id"], event.data["name"], event.data["arguments"])
        for event in events
        if event.type == EventKind.TOOL_CALL.value
    ]
    assert [(name, json.loads(arguments)) for _, name, arguments in tool_calls] == [
        (name, json.loads(arguments)) for name, arguments in recording.tool_calls
    ]
    assert all(TOOL_CALL_ID_RE.fullmatch(call_id) for call_id, _, _ in tool_calls)

    tool_messages = _messages(events, EventKind.TOOL_RESULT)
    assert [(message.text(), message.is_error) for message in tool_messages] == [
        (recorded.content, recorded.is_error) for recorded in recording.tool_results
    ]
    assert [message.tool_call_id for message in tool_messages] == [call_id for call_id, _, _ in tool_calls]


@pytest.mark.parametrize(("case_id", "path"), CASES, ids=CASE_IDS)
async def test_native_loop_emits_only_native_events(case_id: str, path, tmp_path) -> None:
    recording = load_recording(path)
    log, _, _ = await _drive(recording, tmp_path)
    types = [event.type for event in log.events]
    run_types = {kind.value for kind in EventKind}
    assert all(event_type in run_types for event_type in types)
    assert types[0] == EventKind.RUN_STARTED.value
    assert types[-1] == EventKind.RUN_FINISHED.value
    assert types.count(EventKind.RUN_STARTED.value) == len(recording.prompts)
    assert types.count(EventKind.RUN_FINISHED.value) == len(recording.prompts)
