"""Timeline projection for the web surface."""

from __future__ import annotations

from deeppyro.core.messages import (
    Message,
    ModelSource,
    TextPart,
    ToolCallPart,
    ToolSource,
    missing_tool_result_message,
)
from deeppyro.web.service import _timeline_messages

MESSAGE_ID = "msg_0123456789abcdef"
CALL_ID = "call_0123456789abcdef"


def _assistant_call() -> Message:
    return Message(
        id=MESSAGE_ID,
        role="assistant",
        content=(TextPart("working"), ToolCallPart(CALL_ID, "write", '{"content":"x"}')),
        created_at=1,
        source=ModelSource("deepseek", "deepseek-flash"),
    )


def test_a_missing_tool_result_stays_pending() -> None:
    timeline = _timeline_messages([_assistant_call(), missing_tool_result_message(CALL_ID, 2)])

    assert [item["role"] for item in timeline] == ["assistant"]
    call = timeline[0]["tool_calls"][0]
    assert call["result"] is None
    assert call["is_error"] is False
    assert call["pending"] is True


def test_a_real_tool_result_is_joined() -> None:
    assistant = _assistant_call()
    result = Message(
        id="msg_fedcba9876543210",
        role="tool",
        content=(TextPart("written"),),
        created_at=3,
        source=ToolSource(CALL_ID),
        tool_call_id=CALL_ID,
    )

    timeline = _timeline_messages([assistant, result])

    assert [item["role"] for item in timeline] == ["assistant", "tool"]
    call = timeline[0]["tool_calls"][0]
    assert call["result"] == "written"
    assert call["is_error"] is False
    assert call["pending"] is False


def test_a_tool_error_is_joined_as_error() -> None:
    assistant = _assistant_call()
    result = Message(
        id="msg_fedcba9876543210",
        role="tool",
        content=(TextPart("denied"),),
        created_at=3,
        source=ToolSource(CALL_ID),
        tool_call_id=CALL_ID,
        is_error=True,
    )

    timeline = _timeline_messages([assistant, result])

    call = timeline[0]["tool_calls"][0]
    assert call["result"] == "denied"
    assert call["is_error"] is True
    assert call["pending"] is False
