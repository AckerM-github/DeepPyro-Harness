"""Conversation widget tests: streaming, labels, tool cards and thinking."""

from __future__ import annotations

import json
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Markdown, Static

from deeppyro.tui.widgets.conversation import AssistantBlock, ConversationView
from deeppyro.tui.widgets.thinking import ThinkingBlock
from deeppyro.tui.widgets.tool_card import ToolCallCard


class ConversationHost(App[Any]):
    """Minimal host so widget tests do not boot the real runtime."""

    def compose(self) -> ComposeResult:
        yield ConversationView()


def rendered(widget: Static) -> str:
    renderable = widget.render()
    if isinstance(renderable, Text):
        return renderable.plain
    return str(renderable)


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


async def test_thinking_shows_one_rolling_sentence_then_collapses() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("why?")
        conversation.stream_thinking("第一句。second line")
        await pilot.pause(0.2)
        thinking = conversation.query_one(ThinkingBlock)
        assert thinking.finished is False
        live = rendered(thinking)
        assert "second line" in live
        assert "第一句" not in live
        assert len(live.splitlines()) == 1
        assert thinking.display is True

        conversation.finish_assistant("because")
        await pilot.pause()
        assert thinking.finished is True
        collapsed = rendered(thinking)
        assert "· thinking" in collapsed
        assert "second line" not in collapsed
        assert thinking.display is True

        await pilot.click(thinking)
        await pilot.pause()
        assert thinking.expanded is True
        assert "第一句" in rendered(thinking)


async def test_long_thinking_keeps_only_the_newest_sentence() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("q")
        conversation.stream_thinking("\n".join(f"line {index}。" for index in range(12)))
        await pilot.pause(0.2)
        thinking = conversation.query_one(ThinkingBlock)
        text = rendered(thinking)
        assert "line 11" in text
        assert "line 0" not in text
        assert len(text.splitlines()) == 1


async def test_answer_start_collapses_the_reasoning_ticker() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("q")
        conversation.stream_thinking("first thought。second thought。")
        await pilot.pause(0.2)
        thinking = conversation.query_one(ThinkingBlock)
        assert thinking.finished is False

        conversation.stream_text("the answer")
        await pilot.pause(0.2)
        assert thinking.finished is True
        collapsed = rendered(thinking)
        assert "· thinking" in collapsed
        assert "2 sentences" in collapsed
        assert "second thought" not in collapsed


async def test_streaming_sentence_is_single_line_and_tail_truncated() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("q")
        conversation.stream_thinking("开头。" + "很长的思考" * 60 + "结束")
        await pilot.pause(0.2)
        thinking = conversation.query_one(ThinkingBlock)
        text = rendered(thinking)
        assert len(text.splitlines()) == 1
        assert "结束" in text
        assert len(text) <= 130


async def test_tool_call_and_result_render_on_one_card() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("read the file")
        conversation.add_tool_call("call-1", "read", json.dumps({"path": "D:/work/a.py", "offset": 1}))
        await pilot.pause()
        card = conversation.query_one(ToolCallCard)
        assert card.summary == "D:/work/a.py"
        assert "read" in rendered(card.query_one(".tool-call", Static))

        conversation.add_tool_result("call-1", False, "<content>1: hello</content>")
        await pilot.pause()
        result = rendered(card.query_one(".tool-result-line", Static))
        assert "1: hello" in result
        assert "<content>" not in result


async def test_edit_result_renders_diff() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_user("edit it")
        arguments = json.dumps({"path": "a.py", "old": "old line", "new": "new line"})
        conversation.add_tool_call("call-edit", "edit", arguments)
        conversation.add_tool_result("call-edit", False, "edited a.py")
        await pilot.pause()
        card = conversation.query_one(ToolCallCard)
        text = rendered(card.query_one(".tool-result-line", Static))
        assert "- old line" in text
        assert "+ new line" in text


async def test_tool_result_without_call_creates_fallback_card() -> None:
    app = ConversationHost()
    async with app.run_test(size=(100, 30)) as pilot:
        conversation = app.query_one(ConversationView)
        conversation.add_tool_result("orphan", True, "boom")
        await pilot.pause()
        cards = conversation.query(ToolCallCard)
        assert len(cards) == 1
        assert cards.first().is_error is True
        assert "boom" in rendered(cards.first().query_one(".tool-result-line", Static))


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
