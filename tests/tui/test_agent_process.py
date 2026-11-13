"""Tests for the unified agent process block."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Static

from deeppyro.tui.widgets.agent_process import AgentProcess, ToolProcessLine


class ProcessApp(App[None]):
    def __init__(self, process: AgentProcess) -> None:
        super().__init__()
        self.process = process
        self.requests: list[AgentProcess.ThinkingRequested] = []

    def compose(self) -> ComposeResult:
        yield self.process

    def on_agent_process_thinking_requested(self, event: AgentProcess.ThinkingRequested) -> None:
        self.requests.append(event)


def _tool_line(process: AgentProcess) -> ToolProcessLine:
    return process.query_one(ToolProcessLine)


def _step_widgets(process: AgentProcess) -> list[Static]:
    return list(process.query(".process-step"))


async def test_running_header_shows_the_latest_sentence_on_one_line() -> None:
    process = AgentProcess(turn=1)
    process.begin_step(1)
    process.append_thinking("先读取文件。然后分析内容。")
    async with ProcessApp(process).run_test(size=(60, 12)) as pilot:
        await pilot.pause()
        assert process.finished is False
        assert process.expanded is True
        assert "Step 1" in process.header_text
        assert "分析内容" in process.header_text
        assert process.outer_size.height <= 4
        assert len(_step_widgets(process)) == 1


async def test_steps_are_listed_in_begin_order() -> None:
    process = AgentProcess(turn=2)
    process.begin_step(1)
    process.append_thinking("first thought")
    process.begin_step(2)
    process.append_thinking("second thought")
    async with ProcessApp(process).run_test(size=(80, 16)) as pilot:
        await pilot.pause()
        steps = _step_widgets(process)
        assert len(steps) == 2
        assert "first thought" in str(steps[0].content)
        assert "second thought" in str(steps[1].content)
        assert process.step_count == 2


async def test_tool_call_line_shows_summary_and_result_preview() -> None:
    process = AgentProcess(turn=1)
    process.begin_step(1)
    process.append_thinking("read the readme")
    process.add_tool_call("call-1", "read", '{"path": "README.md"}')
    process.add_tool_result("call-1", False, "<content>1: # DeepPyro\n2: python</content>")
    async with ProcessApp(process).run_test(size=(100, 18)) as pilot:
        await pilot.pause()
        line = _tool_line(process)
        text = line.line_text
        assert "read" in text
        assert "README.md" in text
        assert "DeepPyro" in text
        assert line.expanded is False
        assert process.tool_count == 1


async def test_failed_tool_line_marks_the_failure() -> None:
    process = AgentProcess(turn=1)
    process.add_tool_call("call-2", "pwsh", '{"command": "bad"}')
    process.add_tool_result("call-2", True, "exit code 1")
    async with ProcessApp(process).run_test(size=(100, 18)) as pilot:
        await pilot.pause()
        line = _tool_line(process)
        assert line.is_error is True
        assert "failed" in line.line_text
        assert "✗" in line.line_text


async def test_clicking_a_tool_line_expands_full_arguments_and_result() -> None:
    process = AgentProcess(turn=1)
    process.add_tool_call(
        "call-3",
        "edit",
        '{"path": "a.py", "old_string": "old", "new_string": "new"}',
    )
    process.add_tool_result("call-3", False, "updated a.py")
    async with ProcessApp(process).run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        line = _tool_line(process)
        assert line.expanded is False
        await pilot.click(line)
        await pilot.pause()
        assert line.expanded is True
        detail = line.query_one(".process-tool-detail", Static)
        detail_text = str(detail.content)
        assert "old_string" in detail_text
        assert "updated a.py" in detail_text


async def test_finish_collapses_body_and_keeps_a_summary_header() -> None:
    process = AgentProcess(turn=1)
    process.begin_step(1)
    process.append_thinking("one. two.")
    process.add_tool_call("call-1", "read", '{"path": "x"}')
    process.add_tool_result("call-1", False, "ok")
    async with ProcessApp(process).run_test(size=(100, 18)) as pilot:
        await pilot.pause()
        process.finish()
        await pilot.pause()
        assert process.finished is True
        assert process.expanded is False
        body = process.query_one(".process-body")
        assert body.display is False
        assert "agent process" in process.header_text
        assert "1 step" in process.header_text
        assert "1 tool" in process.header_text
        assert "click to view" in process.header_text


async def test_toggle_expands_and_collapses_a_finished_process() -> None:
    process = AgentProcess(turn=1)
    process.begin_step(1)
    process.append_thinking("thought")
    async with ProcessApp(process).run_test(size=(100, 16)) as pilot:
        await pilot.pause()
        process.finish()
        await pilot.pause()
        body = process.query_one(".process-body")
        assert body.display is False
        process.toggle()
        await pilot.pause()
        assert process.expanded is True
        assert body.display is True
        assert "click to collapse" in process.header_text
        process.toggle()
        await pilot.pause()
        assert body.display is False


async def test_clicking_a_step_posts_thinking_requested() -> None:
    process = AgentProcess(turn=1)
    process.begin_step(1)
    process.append_thinking("deep thought")
    app = ProcessApp(process)
    async with app.run_test(size=(100, 16)) as pilot:
        await pilot.pause()
        step = _step_widgets(process)[0]
        await pilot.click(step)
        await pilot.pause()
        assert app.requests
        assert app.requests[0].reasoning is process.reasoning


async def test_empty_process_hides_itself_and_ignores_toggle() -> None:
    process = AgentProcess(turn=1)
    async with ProcessApp(process).run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        assert process.display is False
        assert process.has_content is False
        process.toggle()
        assert process.expanded is True


async def test_long_sentence_is_clipped_to_one_line() -> None:
    process = AgentProcess(turn=1)
    process.begin_step(1)
    process.append_thinking("很长的思考。" * 60)
    async with ProcessApp(process).run_test(size=(40, 12)) as pilot:
        await pilot.pause()
        header = process.query_one(".process-header", Static)
        step = _step_widgets(process)[0]
        assert header.size.height == 1
        assert step.size.height == 1
        assert len(process.header_text) <= 120


def test_calls_before_mount_are_safe_and_are_flushed_on_mount() -> None:
    process = AgentProcess(turn=1)
    process.begin_step(1)
    process.append_thinking("offline thought")
    process.add_tool_call("call-1", "read", '{"path": "x"}')
    process.add_tool_result("call-1", False, "result")
    process.finish()
    assert process.step_count == 1
    assert process.tool_count == 1
    assert "agent process" in process.header_text
