"""Tests for native (schema 1) reasoning and process history reconstruction."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from deeppyro.tui.native_history import (
    ProcessEntry,
    ReasoningStep,
    TurnProcess,
    TurnReasoning,
    TurnTool,
    process_map,
    processes_from_session,
    reasoning_from_session,
    reasoning_map,
    reasoning_text,
)


@dataclass
class Event:
    """One schema 1 event: the type plus the payload the agent loop appended."""

    type: str
    data: dict[str, Any]


class FakeSession:
    """A native session double exposing nothing but its event log."""

    def __init__(self, events: list[Event]) -> None:
        self.events = events


def assistant(*parts: dict[str, Any]) -> Event:
    return Event(
        "assistant.message",
        {"message": {"role": "assistant", "content": list(parts)}},
    )


def tool_result(call_id: str, text: str, *, is_error: bool = False) -> Event:
    return Event(
        "tool.result",
        {
            "message": {
                "role": "tool",
                "tool_call_id": call_id,
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            }
        },
    )


def test_reasoning_text_joins_reasoning_parts_only() -> None:
    message = {
        "content": [
            {"type": "reasoning", "text": "think "},
            {"type": "text", "text": "answer"},
            {"type": "reasoning", "text": "more"},
        ]
    }
    assert reasoning_text(message) == "think more"
    assert reasoning_text({"content": []}) == ""
    assert reasoning_text(None) == ""


def test_reasoning_from_session_groups_by_turn_and_step() -> None:
    session = FakeSession(
        [
            Event("turn.started", {}),
            assistant({"type": "reasoning", "text": "first"}),
            assistant({"type": "reasoning", "text": "second"}),
            Event("turn.ended", {}),
            Event("turn.started", {}),
            assistant({"type": "reasoning", "text": "again"}),
            assistant({"type": "text", "text": "no reasoning here"}),
            Event("turn.ended", {}),
        ]
    )
    turns = reasoning_from_session(session, last_turns=None)
    assert [turn.turn for turn in turns] == [1, 2]
    assert [step.step for step in turns[0].steps] == [1, 2]
    assert [step.text for step in turns[0].steps] == ["first", "second"]
    assert turns[0].finished is True
    assert turns[1].steps == [ReasoningStep(step=1, text="again")]
    assert turns[1].sentence_count == 1
    assert reasoning_map(session)[2].steps[0].text == "again"


def test_step_started_sets_the_step_number() -> None:
    session = FakeSession(
        [
            Event("turn.started", {}),
            Event("step.started", {"turn_id": "t1"}),
            assistant({"type": "reasoning", "text": "a"}),
            Event("step.started", {"turn_id": "t1"}),
            assistant({"type": "reasoning", "text": "b"}),
        ]
    )
    turns = reasoning_from_session(session, last_turns=None)
    assert [step.step for step in turns[0].steps] == [1, 2]


def test_reasoning_from_session_limits_to_latest_turns() -> None:
    events: list[Event] = []
    for turn in (1, 2, 3):
        events.append(Event("turn.started", {}))
        events.append(assistant({"type": "reasoning", "text": f"t{turn}"}))
        events.append(Event("turn.ended", {}))
    session = FakeSession(events)
    assert [turn.turn for turn in reasoning_from_session(session, last_turns=2)] == [2, 3]
    assert [turn.turn for turn in reasoning_from_session(session, last_turns=None)] == [1, 2, 3]
    assert reasoning_from_session(session, last_turns=0) == []


def test_reasoning_from_session_handles_missing_log_and_missing_turn_start() -> None:
    assert reasoning_from_session(object()) == []
    session = FakeSession([assistant({"type": "reasoning", "text": "orphan"})])
    turns = reasoning_from_session(session, last_turns=None)
    assert len(turns) == 1
    assert turns[0].turn == 1
    assert turns[0].finished is False


def test_reasoning_reads_events_through_a_session_handle() -> None:
    session = SimpleNamespace(
        handle=SimpleNamespace(events=[assistant({"type": "reasoning", "text": "wrapped"})])
    )
    assert reasoning_from_session(session, last_turns=None)[0].steps[0].text == "wrapped"


def test_turn_reasoning_text_for() -> None:
    turn = TurnReasoning(
        turn=1,
        steps=[ReasoningStep(step=1, text="a"), ReasoningStep(step=2, text="b")],
    )
    assert turn.text_for(2) == "b"
    assert turn.text_for(3) == ""
    assert turn.has_reasoning is True
    assert TurnReasoning().has_reasoning is False


def test_processes_from_session_interleaves_steps_and_tools() -> None:
    session = FakeSession(
        [
            Event("turn.started", {}),
            assistant(
                {"type": "reasoning", "text": "need the file"},
                {
                    "type": "tool-call",
                    "id": "call-1",
                    "name": "read",
                    "arguments": '{"path": "README.md"}',
                },
            ),
            tool_result("call-1", "line one"),
            assistant({"type": "reasoning", "text": "now answer"}),
            Event("turn.ended", {}),
        ]
    )
    processes = processes_from_session(session, last_turns=None)
    assert len(processes) == 1
    process = processes[0]
    assert process.turn == 1
    assert process.finished is True
    assert [entry.kind for entry in process.entries] == ["step", "tool", "step"]
    assert [entry.step for entry in process.entries] == [1, 1, 2]
    assert process.entries[0].text == "need the file"
    tool = process.entries[1].tool
    assert isinstance(tool, TurnTool)
    assert tool.call_id == "call-1"
    assert tool.name == "read"
    assert tool.result_text == "line one"
    assert tool.has_result is True
    assert tool.is_error is False
    assert [step.step for step in process.reasoning.steps] == [1, 2]


def test_processes_from_session_keeps_tool_only_turns_and_limits() -> None:
    events: list[Event] = []
    for turn in (1, 2, 3):
        events.append(Event("turn.started", {}))
        events.append(
            assistant(
                {
                    "type": "tool-call",
                    "id": f"call-{turn}",
                    "name": "pwsh",
                    "arguments": '{"command": "echo hi"}',
                }
            )
        )
        events.append(tool_result(f"call-{turn}", f"out {turn}", is_error=turn == 2))
        events.append(Event("turn.ended", {}))
    session = FakeSession(events)
    assert [process.turn for process in processes_from_session(session, last_turns=2)] == [2, 3]
    assert processes_from_session(session, last_turns=0) == []
    latest = processes_from_session(session, last_turns=1)[0]
    assert latest.reasoning.has_reasoning is False
    assert latest.tools[0].result_text == "out 3"
    mapping = process_map(session)
    assert mapping[2].tools[0].is_error is True
    assert mapping[1].entries[0].tool is mapping[1].tools[0]


def test_processes_from_session_handles_missing_log() -> None:
    assert processes_from_session(object()) == []
    assert process_map(object()) == {}
    process = TurnProcess(turn=1, entries=[ProcessEntry(kind="tool")])
    assert process.has_content is False
