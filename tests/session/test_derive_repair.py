"""Repairing tool calls a crash left unanswered.

A process killed between "the model asked for a tool" and "the tool result"
leaves an assistant message with tool calls nobody answered. Every later request
then fails with "an assistant message with tool_calls must be followed by tool
messages", which used to poison the session permanently. The repair happens
while reading, so the log stays immutable and old sessions resume.
"""

from __future__ import annotations

from pathlib import Path

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.ids import new_message_id
from deeppyro.core.protocols import Finish, TextDelta
from deeppyro.core.wire import Message, TextPart, ToolCallPart
from deeppyro.session import SessionStore
from deeppyro.session.derive import (
    INTERRUPTED_RESULT_TEXT,
    derive_messages,
    pending_tool_calls,
    repair_pending_tool_calls,
)
from tests.agent.fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)


def assistant_with_call(call_id: str = "call_1", name: str = "echo") -> Message:
    """An assistant message that declares one tool call."""
    return Message(
        id=new_message_id(),
        role="assistant",
        content=[ToolCallPart(id=call_id, name=name, arguments="{}")],
        source={"kind": "assistant"},
    )


def tool_result(call_id: str = "call_1") -> Message:
    """The result message a completed tool call records."""
    return Message(
        id=new_message_id(),
        role="tool",
        toolCallId=call_id,
        content=[TextPart(text="ok")],
        source={"kind": "tool"},
    )


def test_pending_tool_calls_lists_unanswered_calls() -> None:
    messages = [assistant_with_call("call_1", "echo"), assistant_with_call("call_2", "write")]

    assert pending_tool_calls(messages) == [("call_1", "echo"), ("call_2", "write")]


def test_pending_tool_calls_ignores_answered_calls() -> None:
    messages = [assistant_with_call(), tool_result()]

    assert pending_tool_calls(messages) == []


def test_repair_inserts_one_error_result_per_pending_call() -> None:
    messages = [assistant_with_call("call_1", "echo"), assistant_with_call("call_2", "write")]

    repaired = repair_pending_tool_calls(messages)

    assert [message.role for message in repaired] == ["assistant", "tool", "assistant", "tool"]
    first = repaired[1]
    assert first.toolCallId == "call_1"
    assert first.isError is True
    assert first.text() == f"{INTERRUPTED_RESULT_TEXT} (echo)"
    assert first.source == {"kind": "repair"}
    assert repaired[3].toolCallId == "call_2"


def test_repair_keeps_the_result_next_to_its_call() -> None:
    """The API requires the result right after the call, not at the end."""
    later_user = Message(
        id=new_message_id(), role="user", content=[TextPart(text="next")], source={"kind": "user"}
    )
    messages = [assistant_with_call("call_1", "echo"), later_user]

    repaired = repair_pending_tool_calls(messages)

    assert [message.role for message in repaired] == ["assistant", "tool", "user"]
    assert repaired[-1] is later_user


def test_repair_does_not_touch_a_complete_history() -> None:
    messages = [assistant_with_call(), tool_result()]

    repaired = repair_pending_tool_calls(messages)

    assert repaired == messages
    assert repaired is not messages


def test_repair_keeps_the_call_order_of_a_parallel_batch() -> None:
    batch = Message(
        id=new_message_id(),
        role="assistant",
        content=[
            ToolCallPart(id="call_b", name="read", arguments="{}"),
            ToolCallPart(id="call_a", name="read", arguments="{}"),
        ],
        source={"kind": "assistant"},
    )

    repaired = repair_pending_tool_calls([batch])

    assert [
        (message.toolCallId, message.text().rsplit("(", 1)[-1].rstrip(")")) for message in repaired[1:]
    ] == [
        ("call_b", "read"),
        ("call_a", "read"),
    ]


async def test_a_poisoned_log_derives_a_provider_valid_history(tmp_path: Path) -> None:
    """The stored log stays as written; the derived history gains the result."""
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path)
    orphan = Message(
        id=new_message_id(),
        role="assistant",
        content=[ToolCallPart(id="call_1", name="echo", arguments="{}")],
        source={"kind": "model", "provider": "deepseek", "model": "deepseek-flash"},
    )
    session.append(
        "user/message",
        Message(
            id=new_message_id(), role="user", content=[TextPart(text="go")], source={"kind": "user"}
        ).model_dump(),
    )
    session.append("assistant/message", {"message": orphan.model_dump()})

    raw = derive_messages(session.records)
    assert pending_tool_calls(raw) == [("call_1", "echo")]

    repaired = repair_pending_tool_calls(raw)
    assert repaired[-1].toolCallId == "call_1"
    # the durable log is untouched: repair is a read-time projection
    assert [record.type for record in store.open(session.session_id).records][-1] == "assistant/message"


async def test_an_interrupted_session_keeps_answering(tmp_path: Path) -> None:
    """One more turn succeeds even though the log still has an orphan call."""
    session = FakeSessionLog(cwd=tmp_path)
    session.append(
        "user/message",
        Message(
            id=new_message_id(), role="user", content=[TextPart(text="go")], source={"kind": "user"}
        ).model_dump(),
    )
    session.append("assistant/message", {"message": assistant_with_call("call_1", "echo").model_dump()})

    provider = ScriptedProvider([ScriptedTurn(events=[TextDelta("recovered"), finish("stop")])])
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=RecordingSink(session),
        permission=FakePermission(),
        cwd=tmp_path,
        services={},
    )

    assert await agent.send("again") == "recovered"

    sent = provider.requests[0].messages
    repaired = [message for message in sent if message.role == "tool" and message.toolCallId == "call_1"]
    assert repaired, [message.role for message in sent]
    assert repaired[0].isError is True
    assert Finish  # imported for parity with the fake provider's event vocabulary
