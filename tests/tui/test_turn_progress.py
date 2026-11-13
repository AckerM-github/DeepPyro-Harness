"""Tests for the one-line TurnProgress widget."""

from __future__ import annotations

from textual.app import App, ComposeResult

from deeppyro.tui.history import ReasoningStep, TurnReasoning
from deeppyro.tui.widgets.turn_progress import ThinkingRequested, TurnProgress


class ProgressHost(App[None]):
    """Bare application that hosts one progress line and collects clicks."""

    def __init__(self, progress: TurnProgress) -> None:
        super().__init__()
        self.progress = progress
        self.requests: list[TurnReasoning] = []

    def compose(self) -> ComposeResult:
        yield self.progress

    def on_turn_progress_thinking_requested(self, event: TurnProgress.ThinkingRequested) -> None:
        self.requests.append(event.reasoning)


def _reasoning(turn: int = 1) -> TurnReasoning:
    return TurnReasoning(
        turn=turn,
        steps=[ReasoningStep(step=1, text="Alpha! Beta!"), ReasoningStep(step=2, text="Gamma!")],
        finished=True,
    )


def test_widget_contract_and_alias() -> None:
    widget = TurnProgress(turn=7)
    assert widget.has_class("turn-progress")
    assert widget.turn == 7
    assert widget.reasoning.turn == 7
    assert widget.finished is False
    assert widget.step_count == 0
    assert widget.has_text is False
    assert ThinkingRequested is TurnProgress.ThinkingRequested
    assert "_render" not in TurnProgress.__dict__


async def test_streaming_shows_only_the_latest_sentence() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.begin_step(1)
        widget.append("First thought! Second thought! Latest partial")
        await pilot.pause()
        line = widget.line_text
        assert line.startswith("✻ ")
        assert "Latest partial" in line
        assert "First thought" not in line
        assert widget.finished is False


async def test_streaming_trims_a_long_sentence_to_one_line() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.begin_step(1)
        widget.append(" ".join(["word"] * 80))
        await pilot.pause()
        line = widget.line_text
        assert line.startswith("✻ ")
        assert len(line) <= 120
        assert "…" in line
        assert line.endswith("word")


async def test_begin_step_groups_reasoning_per_assistant_step() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.begin_step(1)
        widget.append("Alpha!")
        widget.begin_step(2)
        widget.append("Beta!")
        await pilot.pause()
        assert [step.step for step in widget.reasoning.steps] == [1, 2]
        assert [step.text for step in widget.reasoning.steps] == ["Alpha!", "Beta!"]
        assert widget.step_count == 2
        assert widget.sentence_count == 2


async def test_append_creates_the_first_step_automatically() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.append("")
        await pilot.pause()
        assert widget.step_count == 0
        assert widget.display is False
        widget.append("No explicit step!")
        await pilot.pause()
        assert [step.step for step in widget.reasoning.steps] == [1]
        assert widget.reasoning.steps[0].text == "No explicit step!"
        assert widget.display is True


async def test_finish_renders_the_step_and_sentence_summary() -> None:
    widget = TurnProgress(turn=3)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.begin_step(1)
        widget.append("Alpha! Beta!")
        widget.begin_step(2)
        widget.append("Gamma!")
        widget.finish()
        await pilot.pause()
        line = widget.line_text
        assert widget.finished is True
        assert widget.reasoning.finished is True
        assert "· thinking · 2 steps · 3 sentences" in line
        assert line.endswith("  (click to view)")
        widget.finish()
        await pilot.pause()
        assert widget.line_text == line


async def test_finish_uses_singular_units_for_one_step_and_sentence() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.begin_step(1)
        widget.append("Only one!")
        widget.finish()
        await pilot.pause()
        line = widget.line_text
        assert "1 step" in line
        assert "1 steps" not in line
        assert "1 sentence" in line
        assert "1 sentences" not in line


async def test_click_posts_thinking_requested_with_the_reasoning() -> None:
    widget = TurnProgress(turn=2)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.begin_step(1)
        widget.append("Thinking about it!")
        await pilot.pause()
        await pilot.click(widget)
        await pilot.pause()
        assert len(app.requests) == 1
        assert app.requests[0] is widget.reasoning


async def test_loads_a_finished_turn_reasoning() -> None:
    reasoning = _reasoning(turn=4)
    widget = TurnProgress(reasoning=reasoning)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert widget.turn == 4
        assert widget.reasoning is reasoning
        assert widget.finished is True
        line = widget.line_text
        assert "2 steps" in line
        assert "3 sentences" in line
        assert line.endswith("  (click to view)")


async def test_empty_reasoning_keeps_the_widget_hidden() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert widget.display is False
        assert widget.line_text == ""
        widget.append("Now thinking!")
        await pilot.pause()
        assert widget.display is True
        assert widget.line_text.startswith("✻ ")


async def test_finished_but_empty_reasoning_stays_hidden() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.finish()
        await pilot.pause()
        assert widget.finished is True
        assert widget.display is False


async def test_new_step_without_text_falls_back_to_the_previous_sentence() -> None:
    widget = TurnProgress(turn=1)
    app = ProgressHost(widget)
    async with app.run_test(size=(80, 24)) as pilot:
        widget.begin_step(1)
        widget.append("Old thought!")
        widget.begin_step(2)
        await pilot.pause()
        assert "Old thought!" in widget.line_text
