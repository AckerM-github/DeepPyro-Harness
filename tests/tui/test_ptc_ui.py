"""Tests for the PTC tool rendering and ptc-mode user-message filtering.

The PTC transport turns one model-facing run_code call into a large program plus
a short description. The transcript shows the description on the tool line and
truncates the program in the expanded view. It also records image parts as
internal user messages with source kind ptc-mode; those are model context and
must never appear as user bubbles in the live UI or during history replay.
"""

from __future__ import annotations

import json
from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Static

from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.history import (
    ProcessEntry,
    TurnProcess,
    TurnTool,
    is_ptc_mode_message,
    is_ptc_mode_source,
    source_kind,
)
from deeppyro.tui.widgets.agent_process import AgentProcess, ToolProcessLine
from deeppyro.tui.widgets.conversation import ConversationView
from deeppyro.tui.widgets.tool_card import (
    RUN_CODE_DETAIL_LINES,
    RUN_CODE_NAME,
    run_code_detail,
    summarize_arguments,
)

DESCRIPTION = "Count TODO markers across the repository"
LONG_CODE = "\n".join(f"line-{index:02d} = {index}" for index in range(1, 26))
SHORT_CODE = "print(line-01)"

# --------------------------------------------------------------------------
# Argument rendering helpers
# --------------------------------------------------------------------------


def _run_code_arguments(*, description: str = DESCRIPTION, code: str = LONG_CODE) -> str:
    return json.dumps({"description": description, "code": code, "timeoutMs": 5000})


def test_run_code_summary_prefers_the_description() -> None:
    summary = summarize_arguments(RUN_CODE_NAME, _run_code_arguments())
    assert DESCRIPTION in summary
    assert "line-01" not in summary


def test_run_code_summary_falls_back_without_a_description() -> None:
    summary = summarize_arguments(RUN_CODE_NAME, json.dumps({"code": SHORT_CODE}))
    assert "print" in summary


def test_run_code_summary_falls_back_on_invalid_json() -> None:
    summary = summarize_arguments(RUN_CODE_NAME, "{not valid json")
    assert "{not valid json" in summary


def test_run_code_detail_keeps_the_long_description_and_truncates_code() -> None:
    detail = run_code_detail(_run_code_arguments())
    assert DESCRIPTION in detail
    assert "code (25 lines):" in detail
    assert "line-01 = 1" in detail
    assert "line-20 = 20" in detail
    assert "line-21" not in detail
    assert "... 5 more lines" in detail


def test_run_code_detail_omits_the_counter_for_short_code() -> None:
    detail = run_code_detail(_run_code_arguments(code="a = 1\nb = 2"))
    assert "code (2 lines):" in detail
    assert "more lines" not in detail


def test_run_code_detail_accepts_the_legacy_program_field() -> None:
    detail = run_code_detail(json.dumps({"description": "old record", "program": "x = 1"}))
    assert "old record" in detail
    assert "code (1 lines):" in detail
    assert "x = 1" in detail


def test_run_code_detail_returns_empty_for_unparseable_arguments() -> None:
    assert run_code_detail("not-json{") == ""


def test_run_code_detail_boundary_is_the_declared_line_limit() -> None:
    code = "\n".join(f"line-{index}" for index in range(1, RUN_CODE_DETAIL_LINES + 1))
    detail = run_code_detail(json.dumps({"code": code}))
    assert "more lines" not in detail


# --------------------------------------------------------------------------
# Collapsed tool line and expanded detail (running and replayed turns)
# --------------------------------------------------------------------------


def _detail_text(line: ToolProcessLine) -> str:
    detail = line.query_one(".process-tool-detail", Static)
    plain = getattr(detail.content, "plain", None)
    return str(plain if plain is not None else detail.content)


class ProcessApp(App[None]):
    def __init__(self, process: AgentProcess) -> None:
        super().__init__()
        self.process = process

    def compose(self) -> ComposeResult:
        yield self.process


async def test_tool_line_shows_the_description_and_detail_truncates_code() -> None:
    process = AgentProcess(turn=1)
    process.add_tool_call("call-1", RUN_CODE_NAME, _run_code_arguments())
    async with ProcessApp(process).run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        line = process.query_one(ToolProcessLine)
        line_text = line.line_text
        assert DESCRIPTION in line_text
        assert "line-01" not in line_text
        assert "line-25" not in line_text
        line.toggle()
        await pilot.pause()
        detail = _detail_text(line)
        assert DESCRIPTION in detail
        assert "code (25 lines):" in detail
        assert "line-20 = 20" in detail
        assert "line-21" not in detail
        assert "... 5 more lines" in detail


class ConversationApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ConversationView()


async def test_replayed_history_process_uses_the_same_run_code_summary() -> None:
    tool = TurnTool(call_id="call-9", name=RUN_CODE_NAME, arguments=_run_code_arguments())
    turn = TurnProcess(turn=1)
    turn.tools.append(tool)
    turn.entries.append(ProcessEntry(kind="tool", step=1, tool=tool))
    async with ConversationApp().run_test(size=(120, 20)) as pilot:
        await pilot.pause()
        view = pilot.app.query_one(ConversationView)
        view.add_history_process(turn)
        await pilot.pause()
        line = view.query_one(ToolProcessLine)
        assert DESCRIPTION in line.line_text
        assert "line-01" not in line.line_text


# --------------------------------------------------------------------------
# ptc-mode user messages are hidden
# --------------------------------------------------------------------------


class _ObjectSource:
    kind = "ptc-mode"


def test_ptc_mode_helpers_accept_mappings_objects_and_whitespace() -> None:
    assert is_ptc_mode_source({"kind": "ptc-mode"})
    assert is_ptc_mode_source({"kind": "  ptc-mode  "})
    assert is_ptc_mode_source(_ObjectSource())
    assert source_kind({"kind": "ptc-mode"}) == "ptc-mode"
    assert not is_ptc_mode_source(None)
    assert not is_ptc_mode_source({"kind": "user"})
    assert not is_ptc_mode_source({"kind": 3})
    assert not is_ptc_mode_source({"no-kind": True})
    assert is_ptc_mode_message({"source": {"kind": "ptc-mode"}})
    assert is_ptc_mode_message(type("Message", (), {"source": {"kind": "ptc-mode"}})())
    assert not is_ptc_mode_message({"source": {"kind": "user"}})
    assert not is_ptc_mode_message(None)


async def test_conversation_add_user_skips_ptc_mode_but_keeps_typed_messages() -> None:
    async with ConversationApp().run_test(size=(100, 20)) as pilot:
        await pilot.pause()
        view = pilot.app.query_one(ConversationView)
        view.add_user("hello")
        view.add_user("", source={"kind": "ptc-mode"})
        view.add_user("ignored", source=_ObjectSource())
        view.add_user("world")
        await pilot.pause()
        blocks = list(view.query(".user-block"))
        assert len(blocks) == 2
        text = "".join(
            str(getattr(widget.content, "plain", widget.content)) for widget in view.query(".user-text")
        )
        assert "hello" in text
        assert "world" in text
        assert "ignored" not in text


# --------------------------------------------------------------------------
# History replay keeps turn numbers aligned
# --------------------------------------------------------------------------


class FakeRecord:
    def __init__(self, record_type: str, data: dict[str, Any]) -> None:
        self.type = record_type
        self.data = data


class FakeMessage:
    def __init__(
        self,
        role: str,
        text: str = "",
        *,
        source: dict[str, Any] | None = None,
        content: list[Any] | None = None,
    ) -> None:
        self.role = role
        self.source = source
        self.content = (
            list(content) if content is not None else ([{"type": "text", "text": text}] if text else [])
        )

    def text(self) -> str:
        parts: list[str] = []
        for part in self.content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "".join(parts)


def _assistant_message(text: str, reasoning: str, tool_args: str | None = None) -> FakeMessage:
    content: list[Any] = [{"type": "reasoning", "text": reasoning}, {"type": "text", "text": text}]
    if tool_args is not None:
        content.append({"type": "tool-call", "id": "call-1", "name": RUN_CODE_NAME, "arguments": tool_args})
    return FakeMessage("assistant", content=content)


class FakeHistorySession:
    session_id = "session-ptc-test"

    def __init__(self) -> None:
        self.messages: list[Any] = [
            FakeMessage("user", "hello", source={"kind": "user"}),
            FakeMessage("user", "", source={"kind": "ptc-mode"}),
            _assistant_message("first answer", "thinking one", _run_code_arguments()),
            FakeMessage("user", "again", source={"kind": "user"}),
            _assistant_message("second answer", "thinking two"),
        ]
        self.records: list[Any] = [
            FakeRecord("turn/start", {"turn": 1}),
            FakeRecord(
                "assistant/message",
                {
                    "step": 1,
                    "message": _assistant_message("first answer", "thinking one", _run_code_arguments()),
                },
            ),
            FakeRecord(
                "tool/result",
                {"message": {"toolCallId": "call-1", "content": [{"type": "text", "text": "ok"}]}},
            ),
            FakeRecord("turn/end", {}),
            FakeRecord("turn/start", {"turn": 2}),
            FakeRecord(
                "assistant/message",
                {"step": 1, "message": _assistant_message("second answer", "thinking two")},
            ),
            FakeRecord("turn/end", {}),
        ]

    def derive_messages(self) -> list[Any]:
        return list(self.messages)

    def usage(self) -> dict[str, int]:
        return {"input": 1, "output": 1, "cached": 0}

    def turn_count(self) -> int:
        return 2


class FakePermission:
    preset = "workspace-write"
    approval = "ask"


class FakeRuntime:
    def __init__(self, app: DeepPyroApp) -> None:
        self.sink = app.sink
        self.cwd = "D:/work"
        self.provider_name = "fake"
        self.model = "fake-1"
        self.context_window = 8192
        self.permission = FakePermission()
        self.session = FakeHistorySession()
        self.agent_preset = "standard"
        self.agent_mode = "default"
        self.agent = None

    @property
    def session_id(self) -> str:
        return self.session.session_id

    def session_usage(self) -> dict[str, int]:
        return {"input": 1, "output": 1, "cached": 0}

    async def close(self) -> None:
        return None


async def _make_runtime(app: DeepPyroApp) -> FakeRuntime:
    return FakeRuntime(app)


async def test_history_replay_skips_ptc_mode_and_keeps_turn_numbers() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        conversation = app.conversation
        conversation.clear()
        app._render_history()
        await pilot.pause()
        user_blocks = list(conversation.query(".user-block"))
        assert len(user_blocks) == 2
        user_text = "".join(
            str(getattr(widget.content, "plain", widget.content))
            for widget in conversation.query(".user-text")
        )
        assert "hello" in user_text
        assert "again" in user_text
        processes = list(conversation.query(AgentProcess))
        assert len(processes) == 2
        lines = list(conversation.query(ToolProcessLine))
        assert len(lines) == 1
        assert DESCRIPTION in lines[0].line_text
