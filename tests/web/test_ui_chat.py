"""Pure-function and wiring tests for the NiceGUI chat stage.

The tests deliberately avoid instantiating a NiceGUI page: the streaming and painting
code is exercised through the pure normalisers, the SSE frame parser and the draft state.
"""

from __future__ import annotations

from deeppyro.web.ui.chat import (
    ASSISTANT_NAME,
    POLL_INTERVAL,
    ChatView,
    DraftState,
    MessageState,
    TimelineState,
    ToolStatus,
    append_delta,
    format_tool_arguments,
    header_status,
    normalise_approvals,
    normalise_message,
    normalise_timeline,
    parse_sse_frame,
    source_badge,
    split_slash_command,
    stream_action,
    timeline_signature,
    tool_status,
    tool_status_tone,
)

FRAME_TEXT = b'id: 7\ndata: {"type": "text", "session_id": "s1", "seq": 7, "text": "hello"}\n\n'


def timeline_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "session-1",
        "title": "Refactor the parser",
        "cwd": "C:/code/deeppyro",
        "active": True,
        "busy": False,
        "pending": 0,
        "readable": True,
        "archived": False,
        "provider": "deepseek",
        "model": "deepseek-chat",
        "preset": "standard",
        "permission_preset": "ask",
        "context_tokens": 1200,
        "context_window": 128000,
        "usage": {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 2},
        "journal_seq": 12,
        "messages": [],
    }
    payload.update(overrides)
    return payload


def message_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "m1",
        "role": "assistant",
        "created_at": "2026-01-01T10:00:00Z",
        "text": "Done.",
        "reasoning": "Thought about it.",
        "source": {"kind": "provider", "provider": "deepseek", "model": "deepseek-chat"},
        "tool_call_id": "",
        "is_error": False,
        "images": [],
        "tool_calls": [],
        "has_text": True,
    }
    payload.update(overrides)
    return payload


class FakeBridge:
    """Minimal stand-in: ChatView.__init__ only needs active_session_id."""

    active_session_id = "session-active"


# -- timeline normalisation ---------------------------------------------------


def test_normalise_timeline_reads_the_service_payload() -> None:
    timeline = normalise_timeline(timeline_payload())
    assert timeline.session_id == "session-1"
    assert timeline.title == "Refactor the parser"
    assert timeline.active is True
    assert timeline.busy is False
    assert timeline.journal_seq == 12
    assert timeline.context_tokens == 1200
    assert timeline.context_window == 128000
    assert timeline.usage == {"input_tokens": 10, "output_tokens": 4, "cached_input_tokens": 2}
    assert timeline.messages == ()
    assert timeline.error == ""


def test_normalise_timeline_keeps_producer_errors_as_data() -> None:
    timeline = normalise_timeline({"error": "no session is selected", "messages": [], "readable": False})
    assert timeline.error == "no session is selected"
    assert timeline.messages == ()
    assert timeline.active is False
    assert header_status(timeline) == ("UNAVAILABLE", "danger")


def test_normalise_timeline_tolerates_garbage() -> None:
    timeline = normalise_timeline(None)
    assert timeline.error == "timeline payload is not an object"
    assert normalise_timeline({"usage": "not-a-mapping", "messages": "nope"}).usage == {}


def test_normalise_message_joins_tool_calls_and_marks_pending_results() -> None:
    message = normalise_message(
        message_payload(
            tool_calls=[
                {
                    "id": "call-1",
                    "name": "shell",
                    "arguments": '{"command": "pytest"}',
                    "result": "3 passed",
                    "is_error": False,
                    "pending": False,
                },
                {"id": "call-2", "name": "read", "arguments": "{}", "result": None},
            ]
        )
    )
    assert message is not None
    assert message.role == "assistant"
    assert message.source_kind == "provider"
    assert [call.status for call in message.tool_calls] == [ToolStatus.DONE, ToolStatus.PENDING]
    assert message.tool_calls[0].arguments == '{\n  "command": "pytest"\n}'
    assert message.tool_calls[0].result == "3 passed"
    assert message.tool_calls[1].pending is True


def test_normalise_message_defaults_and_drops_unknown_entries() -> None:
    assert normalise_message("not a message") is None
    message = normalise_message({"text": "hello"})
    assert message is not None
    assert message.role == "assistant"
    assert message.has_text is True
    assert message.tool_calls == ()


def test_normalise_message_collects_image_attachments() -> None:
    message = normalise_message(
        message_payload(images=[{"attachment": "shot.png", "media_type": "image/png"}, {"bad": True}])
    )
    assert message is not None
    assert message.images == ("shot.png",)


def test_normalise_messages_drops_non_objects_inside_timeline() -> None:
    timeline = normalise_timeline(timeline_payload(messages=["junk", message_payload(role="user")]))
    assert len(timeline.messages) == 1
    assert timeline.messages[0].role == "user"


# -- approvals ----------------------------------------------------------------


def test_normalise_approvals_requires_an_id_and_limits_detail() -> None:
    approvals = normalise_approvals(
        [
            {
                "approval_id": "approval-1",
                "session_id": "session-1",
                "tool": "shell",
                "summary": "Run rm -rf build",
                "detail": "x" * 4000,
            },
            {"tool": "shell", "summary": "missing id"},
        ]
    )
    assert len(approvals) == 1
    assert approvals[0].tool == "shell"
    assert approvals[0].detail.endswith("more characters)")
    assert len(approvals[0].detail) < 4000


def test_normalise_approvals_rejects_non_lists() -> None:
    assert normalise_approvals(None) == ()
    assert normalise_approvals({"approval_id": "a"}) == ()


# -- tool status, badges, header status --------------------------------------


def test_tool_status_mapping() -> None:
    assert tool_status(pending=True, is_error=False, result="ok") is ToolStatus.PENDING
    assert tool_status(pending=False, is_error=False, result=None) is ToolStatus.PENDING
    assert tool_status(pending=False, is_error=True, result="boom") is ToolStatus.ERROR
    assert tool_status(pending=False, is_error=False, result="ok") is ToolStatus.DONE


def test_tool_status_tone() -> None:
    assert tool_status_tone(ToolStatus.DONE) == "ok"
    assert tool_status_tone(ToolStatus.ERROR) == "danger"
    assert tool_status_tone(ToolStatus.PENDING) == "warn"


def test_source_badge_mapping() -> None:
    assert source_badge("provider") == ("AGENT", "muted")
    assert source_badge("plugin") == ("PLUGIN", "warn")
    assert source_badge("goal") == ("GOAL", "ok")
    assert source_badge("repair") == ("REPAIR", "danger")
    assert source_badge("") is None
    assert source_badge(None) is None


def test_header_status_prefers_error_and_read_only_states() -> None:
    base = timeline_payload()
    assert header_status(normalise_timeline(base)) == ("IDLE", "muted")
    assert header_status(normalise_timeline(timeline_payload(busy=True))) == ("WORKING", "ok")
    assert header_status(normalise_timeline(timeline_payload(active=False))) == ("READ ONLY", "warn")
    assert header_status(normalise_timeline(timeline_payload(archived=True))) == ("ARCHIVED", "muted")
    assert header_status(normalise_timeline({"error": "gone"})) == ("UNAVAILABLE", "danger")
    assert header_status(TimelineState()) == ("NO SESSION", "muted")


# -- SSE parsing --------------------------------------------------------------


def test_parse_sse_frame_decodes_the_wire_event() -> None:
    event = parse_sse_frame(FRAME_TEXT)
    assert event is not None
    assert event["type"] == "text"
    assert event["session_id"] == "s1"
    assert event["seq"] == 7
    assert event["text"] == "hello"
    assert parse_sse_frame(FRAME_TEXT.decode("utf-8")) == event


def test_parse_sse_frame_joins_multiple_data_lines() -> None:
    frame = b'data: {"type": "final",\ndata: "text": "done"}\n\n'
    assert parse_sse_frame(frame) == {"type": "final", "text": "done"}


def test_parse_sse_frame_ignores_heartbeats_and_garbage() -> None:
    assert parse_sse_frame(b": keep-alive\n\n") is None
    assert parse_sse_frame(b"id: 3\ndata: not-json\n\n") is None
    assert parse_sse_frame(b"data: [1, 2, 3]\n\n") is None
    assert parse_sse_frame(b"id: 3\n\n") is None
    assert parse_sse_frame(b"\xff\xfe") is None
    assert parse_sse_frame(None) is None


def test_stream_action_routing() -> None:
    assert stream_action({"type": "text", "text": "a"}) == "draft-text"
    assert stream_action({"type": "thinking", "text": "t"}) == "draft-thinking"
    assert stream_action({"type": "final", "text": "done"}) == "final"
    assert stream_action({"type": "final", "text": ""}) == "ignore"
    assert stream_action({"type": "text"}) == "ignore"
    assert stream_action({"type": "tool_call"}) == "refresh"
    assert stream_action({"type": "status", "phase": "done"}) == "refresh"
    assert stream_action({"type": "mystery"}) == "ignore"
    assert stream_action(None) == "ignore"


# -- draft / deltas -----------------------------------------------------------


def test_append_delta_keeps_the_tail() -> None:
    assert append_delta("ab", "cd") == "abcd"
    assert append_delta("abc", "") == "abc"
    assert append_delta("abcd", "e", limit=4) == "bcde"


def test_draft_state_tracks_base_sequence_and_settles_once() -> None:
    draft = DraftState()
    assert not draft.has_content
    draft.add_text("Hel", seq=5)
    draft.add_text("lo", seq=6)
    draft.add_reasoning("hmm", seq=6)
    assert draft.base_seq == 5
    assert draft.text == "Hello"
    assert draft.reasoning == "hmm"
    assert draft.has_content
    draft.finish("Hello world", seq=9)
    assert draft.text == "Hello world"
    assert draft.settle_seq == 9
    DraftState().finish("", seq=3)
    assert DraftState().settle_seq is None


def test_format_tool_arguments_pretty_prints_json_only() -> None:
    assert format_tool_arguments('{"b": 1, "a": 2}') == '{\n  "a": 2,\n  "b": 1\n}'
    assert format_tool_arguments("plain text") == "plain text"
    assert format_tool_arguments({"a": [1, 2]}) == '{\n  "a": [\n    1,\n    2\n  ]\n}'
    long_text = "x" * 10_000
    formatted = format_tool_arguments(long_text)
    assert formatted.endswith("more characters)")
    assert len(formatted) < 10_000


# -- slash commands -----------------------------------------------------------


def test_split_slash_command() -> None:
    assert split_slash_command("/help") == ("/help", [])
    assert split_slash_command("  /model deepseek/deepseek-chat  ") == ("/model", ["deepseek/deepseek-chat"])
    assert split_slash_command('/new "my task" now') == ("/new", ["my task", "now"])
    assert split_slash_command("hello") is None
    assert split_slash_command("/") is None
    assert split_slash_command("") is None


# -- timeline signature -------------------------------------------------------


def test_timeline_signature_is_stable_for_equal_payloads() -> None:
    first = normalise_timeline(timeline_payload(messages=[message_payload()]))
    second = normalise_timeline(timeline_payload(messages=[message_payload()]))
    assert timeline_signature(first) == timeline_signature(second)


def test_timeline_signature_changes_when_content_changes() -> None:
    first = normalise_timeline(timeline_payload(messages=[message_payload()]))
    second = normalise_timeline(timeline_payload(messages=[message_payload(text="Different.")]))
    assert timeline_signature(first) != timeline_signature(second)


# -- component wiring ---------------------------------------------------------


def test_chat_view_is_constructible_without_a_page() -> None:
    bridge = FakeBridge()
    view = ChatView(bridge, "session-1", on_session_change=lambda path: None)
    assert view.session_id == "session-1"
    assert ChatView(bridge, None).session_id == "session-active"
    assert isinstance(ChatView(bridge, "session-2"), ChatView)


def test_public_constants_are_stable() -> None:
    assert POLL_INTERVAL == 0.8
    assert ASSISTANT_NAME == "DeepPyro"
    assert MessageState().role == "assistant"
