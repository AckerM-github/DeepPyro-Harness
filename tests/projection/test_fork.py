"""Fork-aware projection initialization tests (inherited event cut)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.projection import ProjectionDefinition, ProjectionRegistry, inherited_event_count
from deeppyro.session.fork import fork_session
from deeppyro.session.store import SessionStore
from tests.projection import support


def _definition(calls: list[tuple[str, str, int]]) -> ProjectionDefinition:
    def init(header: Any, inherited: int) -> dict[str, Any]:
        calls.append(("init", header.id, inherited))
        return {"inherited": inherited, "events": []}

    def apply(state: dict[str, Any], record: Any) -> dict[str, Any]:
        return {"inherited": state["inherited"], "events": state["events"] + [record.seq]}

    return ProjectionDefinition(key="cut", state_version=1, init=init, apply=apply)


def test_synthetic_fork_initializes_from_the_inherited_marker(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path)
    support.append_turn(session, 1)
    support.append_system_prompt(session, "sys")
    support.append_user_message(session, "msg_user", "hello")
    session.append(
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "id": "msg_assistant",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "calling"},
                    {
                        "type": "tool-call",
                        "id": "call-1",
                        "name": "echo",
                        "arguments": "{}",
                    },
                ],
                "source": {"kind": "model", "provider": "p", "model": "m"},
            },
        },
    )
    call_seq = session.append(
        "tool/call",
        {"turn": 1, "step": 1, "callId": "call-1", "name": "echo", "arguments": "{}"},
    )

    child = fork_session(store, session.session_id, at_seq=call_seq)
    assert child.header.isSeeded is True
    assert child.header.parentSession == session.session_id
    assert child.header.delegationDepth == session.header.delegationDepth + 1
    marker = child.records[call_seq + 1]
    assert marker.type == "session/end-seed"
    assert marker.data == {"inherited": True}
    assert inherited_event_count(child) == call_seq + 1
    assert [record.type for record in child.records].count("session/end-seed") == 1
    assert "tool/result" in [record.type for record in child.records]

    calls: list[tuple[str, str, int]] = []
    registry = ProjectionRegistry()
    handle = registry.register(_definition(calls))
    state = handle.state_of(child)
    assert calls[0] == ("init", child.session_id, call_seq + 1)
    assert state["inherited"] == call_seq + 1
    assert state["events"] == [record.seq for record in child.records]


@support.requires_fixtures
def test_real_fork_uses_the_marker_cut(tmp_path: Path) -> None:
    session = support.open_fixture(tmp_path, "session-d745f174")
    store = SessionStore(Path(session.path).parent)
    child = fork_session(store, session.session_id)
    marker_positions = [
        index for index, record in enumerate(child.records) if record.type == "session/end-seed"
    ]
    assert len(marker_positions) == 1
    assert inherited_event_count(child) == marker_positions[0]
    calls: list[tuple[str, str, int]] = []
    registry = ProjectionRegistry()
    handle = registry.register(_definition(calls))
    handle.state_of(child)
    assert calls[0][2] == marker_positions[0]


@support.requires_fixtures
def test_seeded_fixture_without_retained_marker(tmp_path: Path) -> None:
    session = support.open_fixture(tmp_path, "session-66de8981")
    assert session.header.isSeeded is True
    assert session.header.parentSession == "session-c423d78a-a941-4ac9-baec-8ab59ba6462c"
    assert session.header.origin == "subagent"
    assert session.header.delegationDepth == 1
    types = [record.type for record in session.records]
    assert "session/end-seed" not in types
    # The trimmed sample dropped the inherited cut, so the child folds from zero.
    assert inherited_event_count(session) == 0
    calls: list[tuple[str, str, int]] = []
    registry = ProjectionRegistry()
    handle = registry.register(_definition(calls))
    handle.state_of(session)
    assert calls[0][2] == 0
