"""Conversation widget tests: streaming, labels, agent process and tool lines."""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Markdown, Static

from deeppyro.tui.history import (
    ProcessEntry,
    ReasoningStep,
    TurnProcess,
    TurnReasoning,
    TurnTool,
)
from deeppyro.tui.widgets.agent_process import AgentProcess, ToolProcessLine
from deeppyro.tui.widgets.conversation import AssistantBlock, ConversationView


class ConversationHost(App[Any]):
    """Minimal host so widget tests do not boot the real runtime."""

    def compose(self) -> ComposeResult:
        yield ConversationView()


def rendered(widget: Static) -> str:
    renderable = widget.render()
    if isinstance(renderable, Text):
        return renderable.plain
    return str(renderable)


def process_of(conversation: ConversationView) -> AgentProcess:
    return conversation.query_one(AgentProcess)


async def test_stream_text_before_mount_is_not_lost() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        block = AssistantBlock()
        conversation.mount(block)
        # Events can land before the widget finishes mounting.
        block.append_text("early chunk")
        await pilot.pause()
        assert block.text == "early chunk"
        assert block.query_one(Markdown).display is True


async def test_conversation_streams_text_into_one_block() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.stream_text("hello ")
        conversation.stream_text("world")
        await pilot.pause(0.2)
        blocks = conversation.query(AssistantBlock)
        assert len(blocks) == 1
        assert blocks.first().text == "hello world"


async def test_one_assistant_label_per_user_turn() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("first question")
        conversation.stream_text("step one")
        conversation.finish_assistant()
        conversation.stream_text("step two")
        conversation.finish_assistant()
        await pilot.pause()
        assert len(conversation.query(".block-label-assistant")) == 1

        conversation.add_user("second question")
        conversation.stream_text("answer")
        conversation.finish_assistant()
        await pilot.pause()
        assert len(conversation.query(".block-label-assistant")) == 2


async def test_process_streams_one_rolling_sentence_then_collapses() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("why?")
        conversation.begin_step(1, 1)
        conversation.stream_thinking("第一句。second line")
        await pilot.pause(0.2)
        process = process_of(conversation)
        assert process.finished is False
        live = process.header_text
        assert "second line" in live
        assert "第一句" not in live
        assert len(live.splitlines()) == 1
        assert process.display is True

        conversation.finish_turn("because")
        await pilot.pause()
        assert process.finished is True
        collapsed = process.header_text
        assert "agent process" in collapsed
        assert "second line" not in collapsed
        assert "(click to view)" in collapsed
        assert process.display is True


async def test_begin_step_keeps_one_process_widget() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("q")
        conversation.begin_step(1, 1)
        conversation.stream_thinking("first step thought。")
        conversation.add_tool_call("call-1", "read", json.dumps({"path": "a.py"}))
        conversation.add_tool_result("call-1", False, "ok")
        conversation.begin_step(1, 2)
        conversation.stream_thinking("second step thought。")
        await pilot.pause(0.2)
        processes = conversation.query(AgentProcess)
        assert len(processes) == 1
        process = processes.first()
        assert process.step_count == 2
        assert process.tool_count == 1
        assert "second step thought" in process.header_text
        assert process.finished is False


async def test_tools_render_inside_the_same_process_block() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("q")
        conversation.begin_step(1, 1)
        conversation.stream_thinking("planning。")
        conversation.add_tool_call("call-1", "read", json.dumps({"path": "a.py"}))
        conversation.add_tool_result("call-1", False, "ok")
        await pilot.pause()
        process = process_of(conversation)
        assert process.finished is False
        lines = process.query(ToolProcessLine)
        assert len(lines) == 1
        assert "read" in lines.first().line_text
        assert "a.py" in lines.first().line_text


async def test_long_thinking_keeps_only_the_newest_sentence() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("q")
        conversation.begin_step(1, 1)
        conversation.stream_thinking("\n".join(f"line {index}。" for index in range(12)))
        await pilot.pause(0.2)
        text = process_of(conversation).header_text
        assert "line 11" in text
        assert "line 0" not in text
        assert len(text.splitlines()) == 1


async def test_streaming_sentence_is_single_line_and_tail_truncated() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("q")
        conversation.begin_step(1, 1)
        conversation.stream_thinking("开头。" + "很长的思考" * 60 + "结束")
        await pilot.pause(0.2)
        text = process_of(conversation).header_text
        assert len(text.splitlines()) == 1
        assert "结束" in text
        assert len(text) <= 120


async def test_one_process_per_user_turn() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("first")
        conversation.begin_step(1, 1)
        conversation.stream_thinking("first thought。")
        conversation.finish_turn("one")
        conversation.add_user("second")
        conversation.begin_step(2, 1)
        conversation.stream_thinking("second thought。")
        conversation.finish_turn("two")
        await pilot.pause()
        processes = conversation.query(AgentProcess)
        assert len(processes) == 2
        assert processes[0].reasoning.turn == 1
        assert processes[1].reasoning.turn == 2


async def test_history_process_renders_a_collapsed_summary() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        turn = TurnProcess(
            turn=3,
            reasoning=TurnReasoning(
                turn=3,
                steps=[
                    ReasoningStep(step=1, text="first。second。"),
                    ReasoningStep(step=2, text="third。"),
                ],
                finished=True,
            ),
            entries=[
                ProcessEntry(kind="step", step=1, text="first。second。"),
                ProcessEntry(kind="step", step=2, text="third。"),
                ProcessEntry(
                    kind="tool",
                    step=2,
                    tool=TurnTool(
                        call_id="call-1",
                        name="read",
                        arguments=json.dumps({"path": "a.py"}),
                        result_text="1: hello",
                        has_result=True,
                    ),
                ),
            ],
            tools=[
                TurnTool(
                    call_id="call-1",
                    name="read",
                    arguments=json.dumps({"path": "a.py"}),
                    result_text="1: hello",
                    has_result=True,
                )
            ],
            finished=True,
        )
        conversation.add_history_process(turn)
        await pilot.pause()
        process = process_of(conversation)
        assert process.reasoning.turn == 3
        assert process.finished is True
        assert "2 steps" in process.header_text
        assert "1 tool" in process.header_text
        assert "(click to view)" in process.header_text


async def test_tool_call_and_result_render_on_one_line() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("read the file")
        conversation.add_tool_call("call-1", "read", json.dumps({"path": "D:/work/a.py", "offset": 1}))
        await pilot.pause()
        line = conversation.query_one(ToolProcessLine)
        assert "D:/work/a.py" in line.line_text
        assert "read" in line.line_text

        conversation.add_tool_result("call-1", False, "<content>1: hello</content>")
        await pilot.pause()
        assert "1: hello" in line.line_text
        assert "<content>" not in line.line_text


async def test_edit_arguments_are_visible_when_the_tool_line_expands() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("edit it")
        arguments = json.dumps({"path": "a.py", "old": "old line", "new": "new line"})
        conversation.add_tool_call("call-edit", "edit", arguments)
        conversation.add_tool_result("call-edit", False, "edited a.py")
        await pilot.pause()
        line = conversation.query_one(ToolProcessLine)
        assert line.expanded is False
        line.toggle()
        await pilot.pause()
        detail = rendered(line.query_one(".process-tool-detail", Static))
        assert "old line" in detail
        assert "new line" in detail


async def test_tool_result_without_call_creates_fallback_line() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_tool_result("orphan", True, "boom")
        await pilot.pause()
        lines = conversation.query(ToolProcessLine)
        assert len(lines) == 1
        assert lines.first().is_error is True
        assert "boom" in lines.first().line_text


async def test_clear_resets_all_blocks_and_label_state() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("hi")
        conversation.stream_text("hello")
        conversation.finish_assistant()
        await pilot.pause()
        assert len(conversation.children) > 0

        conversation.clear()
        await pilot.pause()
        assert len(conversation.children) == 0

        conversation.add_user("again")
        conversation.stream_text("fresh")
        conversation.finish_assistant()
        await pilot.pause()
        assert len(conversation.query(".block-label-assistant")) == 1
