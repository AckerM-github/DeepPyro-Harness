"""Validation tests for the session event vocabulary."""

from __future__ import annotations

import pytest

from deeppyro.core.errors import SessionError
from deeppyro.session import events
from tests.session import support


def _valid_event(**overrides: object) -> dict[str, object]:
    payload = support.surface("user/message", 2, support.user_message())
    payload.update(overrides)
    return payload


def _valid_header(**overrides: object) -> dict[str, object]:
    payload = {
        "type": "session",
        "version": 4,
        "id": "session-test",
        "createdAt": 1,
        "cwd": "C:/work",
        "isSeeded": False,
        "delegationDepth": 0,
    }
    payload.update(overrides)
    return payload


def test_validate_header_accepts_minimal_and_optional_fields() -> None:
    events.validate_header(_valid_header())
    events.validate_header(
        _valid_header(
            parentSession="session-parent",
            origin="subagent",
            agentPreset="ptc",
            delegationDepth=2,
        )
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"type": "not-session"},
        {"version": 3},
        {"id": ""},
        {"createdAt": -1},
        {"createdAt": True},
        {"isSeeded": "false"},
        {"delegationDepth": -1},
        {"origin": "user"},
        {"cwd": "relative/path"},
        {"cwd": ""},
        {"agentPreset": 3},
        {"unexpected": 1},
    ],
)
def test_validate_header_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(SessionError):
        events.validate_header(_valid_header(**overrides))


@pytest.mark.parametrize("missing", ["type", "version", "id", "createdAt", "isSeeded", "delegationDepth"])
def test_validate_header_requires_every_mandatory_field(missing: str) -> None:
    payload = _valid_header()
    del payload[missing]
    with pytest.raises(SessionError):
        events.validate_header(payload)


def test_normalize_event_accepts_append_with_sources() -> None:
    raw = support.surface(
        "tool/result",
        3,
        support.tool_result(),
        sourceEventSeqs=[2],
    )
    normalized = events.normalize_event(raw)
    assert normalized["surfaceOp"] == "append"
    assert normalized["sourceEventSeqs"] == [2]


def test_normalize_event_requires_surface_op_on_surface_events() -> None:
    raw = support.event("user/message", 0, support.user_message())
    with pytest.raises(SessionError, match="requires surfaceOp"):
        events.normalize_event(raw)


@pytest.mark.parametrize(
    "surface_op",
    [
        "replace",
        {"op": "append", "startSeq": 0, "endSeq": 0},
        {"op": "replace", "startSeq": 0},
        {"op": "replace", "startSeq": 0, "endSeq": 0, "extra": 1},
        {"op": "replace", "startSeq": -1, "endSeq": 0},
        {"op": "replace", "startSeq": True, "endSeq": 0},
        {"op": "replace", "startSeq": 1, "endSeq": 0},
    ],
)
def test_normalize_event_rejects_malformed_surface_ops(surface_op: object) -> None:
    raw = support.surface("user/message", 2, support.user_message(), op=surface_op)
    with pytest.raises(SessionError):
        events.normalize_event(raw)


def test_normalize_event_rejects_replace_referencing_future_events() -> None:
    raw = support.surface(
        "user/message",
        1,
        support.user_message(),
        op={"op": "replace", "startSeq": 0, "endSeq": 1},
    )
    with pytest.raises(SessionError, match="earlier events"):
        events.normalize_event(raw)


def test_normalize_event_rejects_surface_metadata_on_plain_events() -> None:
    with pytest.raises(SessionError, match="cannot carry surfaceOp"):
        events.normalize_event(support.event("turn/start", 0, {"turn": 1}, surfaceOp="append"))
    with pytest.raises(SessionError, match="cannot carry sourceEventSeqs"):
        events.normalize_event(support.event("turn/start", 0, {"turn": 1}, sourceEventSeqs=[0]))


def test_normalize_event_accepts_unknown_only_when_ignorable() -> None:
    raw = support.event("vendor/experimental", 4, {"value": 1})
    with pytest.raises(SessionError, match="unknown event type"):
        events.normalize_event(raw)
    normalized = events.normalize_event({**raw, "ignorable": True})
    assert normalized["ignorable"] is True
    with pytest.raises(SessionError, match="cannot carry surface metadata"):
        events.normalize_event({**raw, "ignorable": True, "surfaceOp": "append"})


def test_normalize_event_rejects_assistant_source_seqs() -> None:
    raw = support.surface("assistant/message", 3, support.assistant_message(), sourceEventSeqs=[2])
    with pytest.raises(SessionError, match="must not carry sourceEventSeqs"):
        events.normalize_event(raw)


@pytest.mark.parametrize(
    "raw",
    [
        support.surface("user/message", 0, {"role": "user", "content": [], "source": {"kind": "user"}}),
        support.surface(
            "user/message",
            0,
            {"id": "m", "role": "assistant", "content": [], "source": {"kind": "user"}},
        ),
        support.surface(
            "user/message",
            0,
            {"id": "m", "role": "user", "content": [], "source": {"kind": ""}},
        ),
        support.surface("system/message", 0, support.system_message()),
        support.surface(
            "assistant/message",
            0,
            {
                "turn": 1,
                "step": 1,
                "message": {
                    "id": "m",
                    "role": "assistant",
                    "content": [],
                    "source": {"kind": "model", "provider": "", "model": "x"},
                },
            },
        ),
    ],
)
def test_normalize_event_rejects_invalid_message_shapes(raw: dict[str, object]) -> None:
    if raw["type"] == "system/message":
        raw["data"]["message"]["source"] = {"kind": "model"}  # type: ignore[index]
    with pytest.raises(SessionError):
        events.normalize_event(raw)


def test_normalize_event_validates_system_and_tool_sources() -> None:
    bad_system = support.surface(
        "system/message",
        0,
        {
            "turn": 1,
            "step": 1,
            "message": {"id": "m", "role": "system", "content": [], "source": {"kind": "model"}},
        },
    )
    with pytest.raises(SessionError, match="system-prompt"):
        events.normalize_event(bad_system)

    bad_tool = support.surface(
        "tool/result",
        0,
        {
            "turn": 1,
            "step": 1,
            "message": {
                "id": "m",
                "role": "tool",
                "toolCallId": "other",
                "isError": False,
                "content": [support.text_part("x")],
                "source": {"kind": "tool", "callId": "call_1"},
            },
        },
    )
    with pytest.raises(SessionError, match="mismatched tool call ids"):
        events.normalize_event(bad_tool)


def test_normalize_event_tool_error_requires_is_error() -> None:
    raw = support.surface(
        "tool/result",
        0,
        {
            **support.tool_result(),
            "error": {"name": "Boom", "code": "BOOM"},
        },
    )
    with pytest.raises(SessionError, match="isError"):
        events.normalize_event(raw)
    raw["data"]["message"]["isError"] = True  # type: ignore[index]
    events.normalize_event(raw)


def test_normalize_event_developer_rules() -> None:
    addition = {"type": "tool-addition", "toolName": "read"}
    with_header = support.developer_message(content=[addition])
    with_header["headerSeq"] = 1
    events.normalize_event(support.surface("developer/message", 2, with_header))

    no_header = support.surface("developer/message", 2, support.developer_message(content=[addition]))
    with pytest.raises(SessionError, match="headerSeq"):
        events.normalize_event(no_header)

    inline = support.developer_message(content=[{**addition, "tool": {"name": "read"}}])
    inline["headerSeq"] = 1
    with pytest.raises(SessionError, match="inline tool"):
        events.normalize_event(support.surface("developer/message", 2, inline))

    assistant = support.assistant_message()
    assistant["message"]["content"].append(addition)
    with pytest.raises(SessionError, match="developer role"):
        events.normalize_event(support.surface("assistant/message", 2, assistant))


def test_request_header_prohibitions() -> None:
    def raw(header: dict[str, object]) -> dict[str, object]:
        return support.event("request/header", 0, {"reason": "initial", "header": header})

    events.normalize_event(raw({"config": {"provider": "deepseek", "model": "x"}}))
    with pytest.raises(SessionError, match="header.system"):
        events.normalize_event(raw({"system": "prompt"}))
    with pytest.raises(SessionError, match="empty tools"):
        events.normalize_event(raw({"tools": []}))
    with pytest.raises(SessionError, match="empty adapterDefaults"):
        events.normalize_event(raw({"adapterDefaults": {}}))


def test_normalize_event_envelope_validation() -> None:
    with pytest.raises(SessionError, match="unexpected envelope field"):
        events.normalize_event({**support.event("turn/start", 0, {"turn": 1}), "extra": 1})
    with pytest.raises(SessionError, match="lacks required field time"):
        events.normalize_event({"type": "turn/start", "seq": 0, "data": {"turn": 1}})
    with pytest.raises(SessionError, match="seq"):
        events.normalize_event(support.event("turn/start", -1, {"turn": 1}))
    with pytest.raises(SessionError, match="data must be a JSON object"):
        events.normalize_event({"type": "turn/start", "seq": 0, "time": 0, "data": []})


def test_source_event_seq_codec_roundtrip() -> None:
    assert events.encode_source_event_seqs([1, 2, 3]) == [[1, 3]]
    assert events.encode_source_event_seqs([1, 2]) == [1, 2]
    assert events.encode_source_event_seqs([1, 2, 3, 7, 8, 9]) == [[1, 3], [7, 9]]
    assert events.decode_source_event_seqs([[1, 3], 7], max_seq=9) == [1, 2, 3, 7]
    with pytest.raises(SessionError, match="unique"):
        events.decode_source_event_seqs([1, 1], max_seq=5)
    with pytest.raises(SessionError, match="earlier events"):
        events.decode_source_event_seqs([5], max_seq=5)
    with pytest.raises(SessionError, match="pair"):
        events.decode_source_event_seqs([[1, 2, 3]], max_seq=9)
    with pytest.raises(SessionError, match="start must not exceed"):
        events.decode_source_event_seqs([[3, 1]], max_seq=9)


def test_header_line_dict_is_canonical() -> None:
    encoded = events.header_line_dict(
        support.header(
            session_id="session-x",
            parentSession="session-parent",
            origin="subagent",
            agentPreset="ptc",
            delegationDepth=1,
        ).model_dump(exclude_none=True)
    )
    assert list(encoded) == [
        "type",
        "version",
        "id",
        "createdAt",
        "cwd",
        "parentSession",
        "isSeeded",
        "origin",
        "delegationDepth",
        "agentPreset",
    ]
    assert events.header_line_dict(support.header(cwd="").model_dump(exclude_none=True)) == {
        "type": "session",
        "version": 4,
        "id": "session-test",
        "createdAt": 1_700_000_000_000,
        "isSeeded": False,
        "delegationDepth": 0,
    }


def test_known_event_types_cover_canonical_compaction_records() -> None:
    for name in ("compaction/start", "compaction/summary", "compaction/end", "compaction/prune"):
        assert name in events.KNOWN_EVENT_TYPES
    assert events.SURFACE_EVENTS == {
        "system/message",
        "developer/message",
        "user/message",
        "assistant/message",
        "tool/result",
    }
    assert events.PROJECTION_EVENTS == {"image/offload"}
