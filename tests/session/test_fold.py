from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.core.messages import (
    CompactionSource,
    Message,
    ModelSource,
    RepairSource,
    SystemSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    UserSource,
)
from deeppyro.goal.types import BlockedReason, GoalSnapshot
from deeppyro.session.errors import (
    MessageError,
    MessageRefError,
    OrderingError,
    ToolPairingError,
)
from deeppyro.session.fold import fold
from deeppyro.session.schema import Event, SessionHeader

TURN = "turn_" + "1" * 16
STEP = "step_" + "2" * 16
RUN = "run_" + "a" * 16
TRACE = "b" * 32
CALL = "call_" + "c" * 16
OTHER_CALL = "call_" + "d" * 16

HEADER = SessionHeader(
    id="session-11111111-2222-3333-4444-555555555555",
    created_at=1000,
    cwd=str(Path.cwd()),
    preset="default",
    parent_id=None,
    origin="user",
    delegation_depth=0,
)


def msg_id(n: int) -> str:
    return "msg_" + f"{n:016x}"


def user_message(n: int, text: str, ts: int = 1000) -> Message:
    return Message(
        id=msg_id(n),
        role="user",
        content=(TextPart(text),),
        created_at=ts,
        source=UserSource(),
    )


def assistant_message(
    n: int,
    text: str | None = None,
    *,
    calls: tuple[ToolCallPart, ...] = (),
    ts: int = 1000,
) -> Message:
    parts: tuple[TextPart | ToolCallPart, ...] = (() if text is None else (TextPart(text),)) + calls
    return Message(
        id=msg_id(n),
        role="assistant",
        content=parts,
        created_at=ts,
        source=ModelSource("deepseek", "deepseek-flash"),
    )


def system_message(n: int, text: str) -> Message:
    return Message(
        id=msg_id(n),
        role="system",
        content=(TextPart(text),),
        created_at=1000,
        source=SystemSource(),
    )


def tool_message(n: int, call_id: str, text: str) -> Message:
    return Message(
        id=msg_id(n),
        role="tool",
        content=(TextPart(text),),
        created_at=1000,
        source=ToolSource(call_id),
        tool_call_id=call_id,
    )


def ev(
    kind: str,
    seq: int,
    data: dict[str, object] | None = None,
    *,
    turn: str | None = None,
    step: str | None = None,
) -> Event:
    return Event(
        type=kind,
        seq=seq,
        ts=1000 + seq,
        data={} if data is None else data,
        run_id=RUN,
        turn_id=turn,
        step_id=step,
        trace_id=TRACE,
    )


def start_turn(seq: int = 1) -> list[Event]:
    return [
        ev("turn.started", seq, turn=TURN),
        ev("step.started", seq + 1, turn=TURN, step=STEP),
    ]


def end_turn(seq: int, reason: dict[str, object] | None = None) -> list[Event]:
    return [
        ev("step.ended", seq, turn=TURN, step=STEP),
        ev("turn.ended", seq + 1, {"reason": {"kind": "completed"} if reason is None else reason}, turn=TURN),
    ]


def tool_call_event(seq: int, *, call_id: str = CALL, step: str = STEP) -> Event:
    return ev(
        "tool.call",
        seq,
        {"call_id": call_id, "name": "read_file", "arguments": '{"path":"a.txt"}'},
        turn=TURN,
        step=step,
    )


def text_call_part() -> ToolCallPart:
    return ToolCallPart(CALL, "read_file", '{"path":"a.txt"}')


def basic_conversation(with_result: bool = True) -> list[Event]:
    user = user_message(1, "hi")
    assistant = assistant_message(2, calls=(text_call_part(),))
    final = assistant_message(3, "done")
    events = [
        ev("system.message", 1, {"message": system_message(9, "be nice").to_dict()}),
        *start_turn(2),
        ev("user.message", 4, {"message": user.to_dict()}, turn=TURN),
        ev("assistant.message", 5, {"message": assistant.to_dict()}, turn=TURN, step=STEP),
        tool_call_event(6),
    ]
    if with_result:
        events.append(
            ev(
                "tool.result",
                7,
                {"call_id": CALL, "message": tool_message(4, CALL, "contents").to_dict()},
                turn=TURN,
                step=STEP,
            )
        )
    events.append(ev("assistant.message", 8, {"message": final.to_dict()}, turn=TURN, step=STEP))
    events.extend(end_turn(9))
    return events


def test_basic_conversation_projects_messages_and_state() -> None:
    events = basic_conversation()
    view = fold(HEADER, events)

    assert view.header == HEADER
    assert view.system is not None
    assert view.system.id == msg_id(9)
    assert [message.id for message in view.messages] == [msg_id(1), msg_id(2), msg_id(4), msg_id(3)]
    assert view.messages[1].tool_calls()[0].id == CALL
    assert view.messages[2].tool_call_id == CALL
    assert view.last_message_id == msg_id(3)
    assert view.last_seq == events[-1].seq
    assert view.issues == ()
    assert view.pending_approvals == ()
    assert view.state.cwd == HEADER.cwd
    assert view.state.preset is not None
    assert view.state.preset.preset == "default"
    assert view.state.preset.mode == "default"


def test_missing_tool_result_on_completed_turn_is_an_error() -> None:
    with pytest.raises(ToolPairingError, match=r"has no matching tool.result"):
        fold(HEADER, basic_conversation(with_result=False))


def test_aborted_turn_derives_repair_message() -> None:
    events = basic_conversation(with_result=False)
    events[-1] = ev("turn.ended", events[-1].seq, {"reason": {"kind": "aborted", "by": "user"}}, turn=TURN)
    view = fold(HEADER, events)

    repair = view.messages[-1]
    assert repair.id == "msg_missing_" + CALL.removeprefix("call_")
    assert repair.role == "tool"
    assert repair.tool_call_id == CALL
    assert repair.is_error is True
    assert isinstance(repair.source, RepairSource)
    assert repair.source.reason == "missing-tool-result"
    assert len(view.issues) == 1
    issue = view.issues[0]
    assert issue.code == "missing-tool-result"
    assert issue.call_id == CALL
    assert issue.seq == events[-1].seq


def test_open_turn_is_repaired_at_end_of_log() -> None:
    events = basic_conversation(with_result=False)
    events = events[:-2]  # drop step.ended and turn.ended
    view = fold(HEADER, events)

    assert view.messages[-1].id == "msg_missing_" + CALL.removeprefix("call_")
    assert view.issues[0].seq == events[-1].seq


def test_error_turn_is_repaired() -> None:
    events = basic_conversation(with_result=False)
    events[-1] = ev(
        "turn.ended",
        events[-1].seq,
        {"reason": {"kind": "error", "message": "boom"}},
        turn=TURN,
    )
    view = fold(HEADER, events)
    assert len(view.issues) == 1


def test_tool_call_needs_assistant_call() -> None:
    events = [*start_turn(), tool_call_event(3)]
    with pytest.raises(ToolPairingError, match="no matching assistant tool call"):
        fold(HEADER, events)


def test_tool_call_must_match_step_and_payload() -> None:
    assistant = assistant_message(1, calls=(text_call_part(),))
    events = [
        *start_turn(),
        ev("assistant.message", 3, {"message": assistant.to_dict()}, turn=TURN, step=STEP),
        tool_call_event(4, step="step_" + "9" * 16),
    ]
    with pytest.raises(ToolPairingError, match="not in the same turn and step"):
        fold(HEADER, events)

    events[-1] = ev(
        "tool.call",
        4,
        {"call_id": CALL, "name": "write_file", "arguments": '{"path":"a.txt"}'},
        turn=TURN,
        step=STEP,
    )
    with pytest.raises(ToolPairingError, match="does not match the assistant tool call"):
        fold(HEADER, events)


def test_tool_result_needs_call() -> None:
    events = [
        ev(
            "tool.result",
            1,
            {"call_id": CALL, "message": tool_message(1, CALL, "x").to_dict()},
            turn=TURN,
            step=STEP,
        )
    ]
    with pytest.raises(ToolPairingError, match=r"no matching tool.call"):
        fold(HEADER, events)


def test_duplicate_call_id_is_rejected() -> None:
    first = assistant_message(1, calls=(text_call_part(),))
    second = assistant_message(2, calls=(text_call_part(),))
    events = [
        *start_turn(),
        ev("assistant.message", 3, {"message": first.to_dict()}, turn=TURN, step=STEP),
        ev("assistant.message", 4, {"message": second.to_dict()}, turn=TURN, step=STEP),
    ]
    with pytest.raises(ToolPairingError, match="used more than once"):
        fold(HEADER, events)


def test_message_replaced_updates_content_in_place() -> None:
    first = user_message(1, "old")
    second = user_message(2, "keep")
    replacement = user_message(1, "new")
    events = [
        ev("user.message", 1, {"message": first.to_dict()}, turn=TURN),
        ev("user.message", 2, {"message": second.to_dict()}, turn=TURN),
        ev("message.replaced", 3, {"message_id": msg_id(1), "message": replacement.to_dict()}),
    ]
    view = fold(HEADER, events)
    assert [message.text() for message in view.messages] == ["new", "keep"]


def test_message_replaced_cannot_change_identity() -> None:
    first = user_message(1, "old")
    replacement = Message(
        id=msg_id(1),
        role="assistant",
        content=(TextPart("new"),),
        created_at=1000,
        source=ModelSource("deepseek", "deepseek-flash"),
    )
    events = [
        ev("user.message", 1, {"message": first.to_dict()}, turn=TURN),
        ev("message.replaced", 2, {"message_id": msg_id(1), "message": replacement.to_dict()}),
    ]
    with pytest.raises(MessageError, match="may only change content and is_error"):
        fold(HEADER, events)


def test_message_replaced_unknown_id_is_rejected() -> None:
    events = [
        ev(
            "message.replaced",
            1,
            {"message_id": msg_id(1), "message": user_message(1, "new").to_dict()},
        )
    ]
    with pytest.raises(MessageRefError, match="references unknown message id"):
        fold(HEADER, events)


def test_replaced_system_message_keeps_system_slot() -> None:
    original = system_message(1, "old")
    replacement = system_message(1, "new")
    events = [
        ev("system.message", 1, {"message": original.to_dict()}),
        ev("message.replaced", 2, {"message_id": msg_id(1), "message": replacement.to_dict()}),
    ]
    view = fold(HEADER, events)
    assert view.system is not None
    assert view.system.text() == "new"
    assert view.messages == ()


def test_compaction_replaces_range_with_summary() -> None:
    messages = [user_message(1, "one"), assistant_message(2, "two"), user_message(3, "three")]
    summary = Message(
        id=msg_id(4),
        role="user",
        content=(TextPart("summary"),),
        created_at=1000,
        source=CompactionSource("compaction_1"),
    )
    events = [
        ev("user.message", 1, {"message": messages[0].to_dict()}, turn=TURN),
        ev("assistant.message", 2, {"message": messages[1].to_dict()}, turn=TURN, step=STEP),
        ev("user.message", 3, {"message": messages[2].to_dict()}, turn=TURN),
        ev(
            "compaction.done",
            4,
            {"covers": [msg_id(1), msg_id(2)], "message": summary.to_dict()},
            turn=TURN,
        ),
    ]
    view = fold(HEADER, events)
    assert [message.id for message in view.messages] == [msg_id(4), msg_id(3)]
    assert view.messages[0].text() == "summary"


def test_compaction_rejects_non_contiguous_cover() -> None:
    events = [
        ev("user.message", 1, {"message": user_message(1, "one").to_dict()}, turn=TURN),
        ev("assistant.message", 2, {"message": assistant_message(2, "two").to_dict()}, turn=TURN, step=STEP),
        ev("user.message", 3, {"message": user_message(3, "three").to_dict()}, turn=TURN),
        ev(
            "compaction.done",
            4,
            {
                "covers": [msg_id(1), msg_id(3)],
                "message": Message(
                    id=msg_id(4),
                    role="user",
                    content=(TextPart("summary"),),
                    created_at=1000,
                    source=CompactionSource("compaction_1"),
                ).to_dict(),
            },
            turn=TURN,
        ),
    ]
    with pytest.raises(MessageRefError, match="not adjacent"):
        fold(HEADER, events)


def test_compaction_covered_message_cannot_be_replaced() -> None:
    summary = Message(
        id=msg_id(2),
        role="user",
        content=(TextPart("summary"),),
        created_at=1000,
        source=CompactionSource("compaction_1"),
    )
    events = [
        ev("user.message", 1, {"message": user_message(1, "one").to_dict()}, turn=TURN),
        ev(
            "compaction.done",
            2,
            {"covers": [msg_id(1)], "message": summary.to_dict()},
            turn=TURN,
        ),
        ev(
            "message.replaced",
            3,
            {"message_id": msg_id(1), "message": user_message(1, "new").to_dict()},
        ),
    ]
    with pytest.raises(MessageRefError, match="references unknown message id"):
        fold(HEADER, events)


def goal_payload(goal: GoalSnapshot) -> dict[str, object]:
    data: dict[str, object] = {
        "id": goal.id,
        "revision": goal.revision,
        "objective": goal.objective,
        "phase": goal.phase,
        "max_goal_rounds": goal.max_goal_rounds,
    }
    if goal.blocked_reason is not None:
        data["blocked_reason"] = goal.blocked_reason.to_dict()
    return data


def test_goal_and_state_last_wins() -> None:
    goal = GoalSnapshot(
        id="goal_1",
        revision=1,
        objective="ship it",
        phase="active",
        max_goal_rounds=256,
    )
    blocked = GoalSnapshot(
        id="goal_1",
        revision=2,
        objective="ship it",
        phase="blocked",
        max_goal_rounds=256,
        blocked_reason=BlockedReason("needs-input", "say yes"),
    )
    events = [
        ev("goal.changed", 1, {"goal": goal_payload(goal), "rounds_started": 1}),
        ev("goal.changed", 2, {"goal": goal_payload(blocked), "rounds_started": 2}),
        ev("model.selected", 3, {"provider": "deepseek", "model": "deepseek-flash"}),
        ev("preset.selected", 4, {"preset": "review", "mode": "team", "revision": 3, "previous": "default"}),
        ev("permission.policy", 5, {"policy": "never"}),
        ev("sandbox.mode", 6, {"mode": "workspace-write"}),
        ev("workspace.cwd_changed", 7, {"cwd": str(Path.cwd())}),
        ev("session.title", 8, {"title": "M1", "source": "user"}),
        ev("plan.mode", 9, {"mode": "execute"}),
        ev("todo.written", 10, {"todos": [{"content": "a", "status": "pending"}]}),
        ev(
            "deliverable.presented",
            11,
            {"files": [{"path": "out.txt", "description": "result"}]},
        ),
    ]
    view = fold(HEADER, events)
    assert view.state.goal == blocked
    assert view.state.rounds_started == 2
    assert view.state.model is not None and view.state.model.model == "deepseek-flash"
    assert view.state.preset is not None and view.state.preset.mode == "team"
    assert view.state.permission_policy == "never"
    assert view.state.sandbox_mode == "workspace-write"
    assert view.state.cwd == str(Path.cwd())
    assert view.state.title == "M1"
    assert view.state.title_source == "user"
    assert view.state.plan_mode == "execute"
    assert view.state.todos[0].content == "a"
    assert view.state.deliverables[0].path == "out.txt"


def test_approval_lifecycle() -> None:
    events = [
        ev(
            "approval.requested",
            1,
            {
                "approval_id": "approval_1",
                "tool_call_id": CALL,
                "tool_name": "shell",
                "reason": "needs network",
            },
            turn=TURN,
            step=STEP,
        ),
        ev("approval.decided", 2, {"approval_id": "approval_1", "outcome": "allowed"}, turn=TURN, step=STEP),
    ]
    view = fold(HEADER, events)
    assert view.pending_approvals == ()


def test_pending_approval_is_reported() -> None:
    events = [
        ev(
            "approval.requested",
            1,
            {"approval_id": "approval_1", "tool_call_id": CALL, "tool_name": "shell"},
            turn=TURN,
            step=STEP,
        )
    ]
    view = fold(HEADER, events)
    assert len(view.pending_approvals) == 1
    assert view.pending_approvals[0].tool_call_id == CALL
    assert view.pending_approvals[0].outcome is None


def test_approval_decision_without_request_is_rejected() -> None:
    events = [
        ev("approval.decided", 1, {"approval_id": "approval_1", "outcome": "allowed"}, turn=TURN, step=STEP)
    ]
    with pytest.raises(OrderingError, match="is not pending"):
        fold(HEADER, events)


def test_plugin_events_are_preserved() -> None:
    event = ev("plugin.demo.note", 1, {"value": 1})
    view = fold(HEADER, [event])
    assert view.plugin_events == (event,)
    assert view.messages == ()


def test_fold_is_deterministic() -> None:
    events = basic_conversation()
    assert fold(HEADER, events) == fold(HEADER, events)


def test_invalid_message_payload_is_a_message_error() -> None:
    events = [ev("user.message", 1, {"message": {"id": "bad"}}, turn=TURN)]
    with pytest.raises(MessageError, match="message is invalid"):
        fold(HEADER, events)
