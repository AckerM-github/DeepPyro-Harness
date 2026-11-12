"""Canonical surface fold and message derivation tests."""

from __future__ import annotations

import pathlib

import pytest

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord, Message
from deeppyro.session import derive, reader
from deeppyro.session.derive import (
    APPEND,
    REPLACE,
    SKIP,
    event_message,
    fold_surface,
    plan_surface_transition,
)
from tests.session import support

MESSAGES_DIR = support.SESSIONS_DIR


def _d745() -> pathlib.Path:
    return MESSAGES_DIR / "session-d745f174-c79f-4b69-a147-d6c57947e3f7.jsonl"


def _ddb1ba3d() -> pathlib.Path:
    return MESSAGES_DIR / "session-ddb1ba3d-d055-48c1-9b60-2ad5897dd31b.jsonl"


def _04a44e0f() -> pathlib.Path:
    return MESSAGES_DIR / "session-04a44e0f-a7d5-494c-bb36-a15d62d952f2.jsonl"


def _append_chain() -> list:
    return [
        support.record(support.surface("system/message", 0, support.system_message())),
        support.record(support.surface("user/message", 1, support.user_message("msg_u1", "one"))),
        support.record(support.surface("user/message", 2, support.user_message("msg_u2", "two"))),
    ]


@support.requires_fixtures
@pytest.mark.parametrize("path", support.session_files(), ids=lambda p: p.stem)
def test_strict_derive_every_fixture(path: pathlib.Path) -> None:
    result = reader.read_session_file(path)
    messages = derive.derive_messages(result.records)
    assert all(isinstance(message, Message) for message in messages)
    assert {message.role for message in messages} <= {"system", "user", "assistant", "tool"}
    assert all(message.id for message in messages)
    assert len({message.id for message in messages}) == len(messages)


@support.requires_fixtures
def test_system_prompt_replacement_keeps_head_position() -> None:
    result = reader.read_session_file(_ddb1ba3d())
    replacement = next(
        record
        for record in result.records
        if record.type == "system/message" and record.surfaceOp != "append"
    )
    messages = derive.derive_messages(result.records)
    assert messages[0].role == "system"
    assert messages[0].id == replacement.data["message"]["id"]
    assert replacement.sourceEventSeqs == [8]


@support.requires_fixtures
def test_developer_retirement_hides_shadowed_user_message() -> None:
    result = reader.read_session_file(_d745())
    retired = next(record for record in result.records if record.seq == 13)
    assert retired.data["id"] == "23f3d526-28a4-4321-936d-69aedfe2315d"
    messages = derive.derive_messages(result.records)
    assert all(message.id != retired.data["id"] for message in messages)
    assert "developer" not in {message.role for message in messages}


@support.requires_fixtures
def test_multiple_developer_retirements() -> None:
    result = reader.read_session_file(_04a44e0f())
    retired_ids = {
        next(record for record in result.records if record.seq == target).data["id"]
        for target in (13, 78, 170, 179)
    }
    messages = derive.derive_messages(result.records)
    assert retired_ids.isdisjoint({message.id for message in messages})
    assert "developer" not in {message.role for message in messages}


def test_replace_splices_in_place() -> None:
    records = _append_chain()
    replacement = support.record(
        support.surface(
            "user/message",
            3,
            support.user_message("msg_u3", "three"),
            op={"op": "replace", "startSeq": 1, "endSeq": 1},
            sourceEventSeqs=[1],
        )
    )
    nodes = fold_surface([*records, replacement])
    assert [node.seq for node in nodes] == [0, 3, 2]
    assert [event_message(node).id for node in nodes] == ["msg_system", "msg_u3", "msg_u2"]


def test_replace_span_removes_every_shadowed_node() -> None:
    records = _append_chain()
    replacement = support.record(
        support.surface(
            "user/message",
            3,
            support.user_message("msg_u3", "merged"),
            op={"op": "replace", "startSeq": 1, "endSeq": 2},
            sourceEventSeqs=[1, 2],
        )
    )
    nodes = fold_surface([*records, replacement])
    assert [node.seq for node in nodes] == [0, 3]


def test_missing_replace_target_strict_raises_and_tolerant_appends() -> None:
    records = _append_chain()
    orphan = support.record(
        support.surface(
            "user/message",
            6,
            support.user_message("msg_u3", "orphan"),
            op={"op": "replace", "startSeq": 5, "endSeq": 5},
            sourceEventSeqs=[5],
        )
    )
    with pytest.raises(SessionError, match="not found in surface"):
        fold_surface([*records, orphan])
    nodes = fold_surface([*records, orphan], strict=False)
    assert [node.seq for node in nodes] == [0, 1, 2, 6]


def test_replacement_must_cover_shadowed_nodes() -> None:
    records = _append_chain()
    replacement = support.record(
        support.surface(
            "user/message",
            3,
            support.user_message("msg_u3", "broken"),
            op={"op": "replace", "startSeq": 1, "endSeq": 2},
            sourceEventSeqs=[1],
        )
    )
    with pytest.raises(SessionError, match="must include every shadowed"):
        fold_surface([*records, replacement])


def test_replacement_without_source_event_seqs_is_rejected() -> None:
    records = _append_chain()
    replacement = support.record(
        support.surface(
            "user/message",
            3,
            support.user_message("msg_u3", "broken"),
            op={"op": "replace", "startSeq": 1, "endSeq": 1},
        )
    )
    with pytest.raises(SessionError, match="must include every shadowed"):
        fold_surface([*records, replacement])


def _tool_exchange(*, is_error: bool = False) -> list:
    return [
        support.record(support.surface("system/message", 0, support.system_message())),
        support.record(
            support.surface(
                "assistant/message",
                1,
                support.assistant_message(tool_calls=[{"id": "call_1", "name": "read", "arguments": "{}"}]),
            )
        ),
        support.record(support.event("tool/call", 2, support.tool_call())),
        support.record(
            support.surface(
                "tool/result",
                3,
                support.tool_result("call_1", "old", meta={"cwd": "C:/work"}),
                sourceEventSeqs=[2],
            )
        ),
    ]


def test_tool_result_rewrite_may_change_only_content() -> None:
    records = _tool_exchange()
    rewritten = support.tool_result("call_1", "new", meta={"cwd": "C:/work"})
    replacement = support.record(
        support.surface(
            "tool/result",
            4,
            rewritten,
            op={"op": "replace", "startSeq": 3, "endSeq": 3},
            sourceEventSeqs=[3],
        )
    )
    nodes = fold_surface([*records, replacement])
    assert nodes[-1].seq == 4
    assert event_message(nodes[-1]).text() == "new"

    tampered = support.tool_result("call_1", "new", message_id="other", meta={"cwd": "C:/work"})
    bad = support.record(
        support.surface(
            "tool/result",
            4,
            tampered,
            op={"op": "replace", "startSeq": 3, "endSeq": 3},
            sourceEventSeqs=[3],
        )
    )
    with pytest.raises(SessionError, match="may change only content"):
        fold_surface([*records, bad])


def test_tool_result_rewrite_target_must_be_tool_result() -> None:
    records = _append_chain()
    replacement = support.record(
        support.surface(
            "tool/result",
            3,
            support.tool_result("call_1", "x"),
            op={"op": "replace", "startSeq": 1, "endSeq": 1},
            sourceEventSeqs=[1],
        )
    )
    with pytest.raises(SessionError, match="must target a current tool/result"):
        fold_surface([*records, replacement])


def test_system_head_may_only_be_rewritten_by_one_system_message() -> None:
    records = _append_chain()
    wrong_kind = support.record(
        support.surface(
            "user/message",
            3,
            support.user_message("msg_u3", "hijack"),
            op={"op": "replace", "startSeq": 0, "endSeq": 0},
            sourceEventSeqs=[0],
        )
    )
    with pytest.raises(SessionError, match="node 0 holds the system prompt"):
        fold_surface([*records, wrong_kind])

    two_nodes = support.record(
        support.surface(
            "system/message",
            3,
            support.system_message("msg_system2", "new prompt"),
            op={"op": "replace", "startSeq": 0, "endSeq": 1},
            sourceEventSeqs=[0, 1],
        )
    )
    with pytest.raises(SessionError, match="node 0 holds the system prompt"):
        fold_surface([*records, two_nodes])

    ok = support.record(
        support.surface(
            "system/message",
            3,
            support.system_message("msg_system2", "new prompt"),
            op={"op": "replace", "startSeq": 0, "endSeq": 0},
            sourceEventSeqs=[0],
        )
    )
    nodes = fold_surface([*records, ok])
    assert [node.seq for node in nodes] == [3, 1, 2]
    assert event_message(nodes[0]).text() == "new prompt"


def test_empty_content_projections_are_dropped() -> None:
    records = [
        support.record(support.surface("system/message", 0, support.system_message())),
        support.record(support.surface("developer/message", 1, support.developer_message())),
        support.record(
            support.surface(
                "assistant/message",
                2,
                {
                    "turn": 1,
                    "step": 1,
                    "message": {
                        "id": "msg_empty",
                        "role": "assistant",
                        "content": [],
                        "source": {"kind": "model", "provider": "deepseek", "model": "deepseek-chat"},
                    },
                },
            )
        ),
        support.record(
            support.surface(
                "user/message",
                3,
                {"id": "msg_blank", "role": "user", "content": [], "source": {"kind": "user"}},
            )
        ),
    ]
    nodes = fold_surface(records)
    assert [node.seq for node in nodes] == [0, 1, 2, 3]
    assert event_message(nodes[1]) is None
    assert event_message(nodes[2]) is None
    assert event_message(nodes[3]) is not None


def test_non_surface_and_unknown_ignorable_events_are_skipped() -> None:
    records = [
        support.record(support.event("turn/start", 0, {"turn": 1})),
        support.record(support.event("assistant/attempt", 1, {"turn": 1, "step": 1, "stream": []})),
        support.record(support.event("vendor/experimental", 2, {"value": 1}, ignorable=True)),
        support.record(support.event("turn/end", 3, {"turn": 1})),
        support.record(support.surface("user/message", 4, support.user_message())),
    ]
    nodes = fold_surface(records)
    assert [node.seq for node in nodes] == [4]
    assert len(derive.derive_messages(records)) == 1


def test_image_offload_is_never_a_surface_node() -> None:
    payload = support.event("image/offload", 0, {"attachment": "att_1"})
    nodes = fold_surface([support.record(payload)])
    assert nodes == []


def test_plan_surface_transition_reports_actions() -> None:
    nodes = _append_chain()
    assert (
        plan_surface_transition(
            nodes, support.record(support.event("turn/start", 3, {"turn": 1})), strict=True, records_by_seq={}
        )[0]
        == SKIP
    )
    appended = support.record(support.surface("user/message", 3, support.user_message("msg_u3")))
    assert plan_surface_transition(nodes, appended, strict=True, records_by_seq={})[0] == APPEND
    replaced = support.record(
        support.surface(
            "user/message",
            3,
            support.user_message("msg_u3"),
            op={"op": "replace", "startSeq": 1, "endSeq": 1},
            sourceEventSeqs=[1],
        )
    )
    assert plan_surface_transition(nodes, replaced, strict=True, records_by_seq={})[0] == REPLACE


def test_missing_surface_op_strict_raises_tolerant_skips() -> None:
    # Bypass normalize_event: the reader refuses this record, but the fold's own
    # strict mode must reject it too.
    record = LogRecord.model_validate(support.event("user/message", 0, support.user_message()))
    with pytest.raises(SessionError, match="requires a surfaceOp"):
        fold_surface([record])
    assert fold_surface([record], strict=False) == []
