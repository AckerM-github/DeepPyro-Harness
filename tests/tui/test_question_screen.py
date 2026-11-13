"""Tests for the ask_user_question QuestionScreen modal and app paging."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Static

from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.runtime import TuiRuntime
from deeppyro.tui.screens.question import (
    CANCEL_BUTTON_ID,
    SKIP_BUTTON_ID,
    QuestionOptionRow,
    QuestionScreen,
)
from deeppyro.user_questions.types import Answer, AskRequest, Question, QuestionOption
from tests.tui.test_app import FakeRuntime


class QuestionHost(App[None]):
    """Minimal host that pushes a QuestionScreen and records its result."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[Any] = []

    def compose(self) -> ComposeResult:
        yield Static("host")


def make_question(
    options: int = 2,
    *,
    multi_select: bool = False,
    header: str | None = None,
    question_id: str = "q-1",
) -> Question:
    return Question(
        id=question_id,
        question="Which target should be used?",
        header=header,
        options=tuple(
            QuestionOption(label="option-" + str(position + 1), description="note-" + str(position + 1))
            for position in range(options)
        ),
        multi_select=multi_select,
    )


async def open_question(
    pilot: Any,
    host: QuestionHost,
    question: Question,
    *,
    index: int = 0,
    total: int = 1,
) -> QuestionScreen:
    host.push_screen(QuestionScreen(question, index=index, total=total), callback=host.results.append)
    await pilot.pause()
    screen = host.screen
    assert isinstance(screen, QuestionScreen)
    return screen


def rows_of(screen: QuestionScreen) -> list[QuestionOptionRow]:
    return list(screen.query(QuestionOptionRow))


async def test_screen_renders_question_options_and_hint() -> None:
    host = QuestionHost()
    question = make_question(options=3, header="Pick a target")
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, question)
        title = screen.query_one("#question-title", Static)
        assert "Pick a target" in str(title.content)
        body = screen.query_one("#question-body", Static)
        assert "Which target" in str(body.content)
        rows = rows_of(screen)
        assert len(rows) == 3
        assert "option-1" in rows[0].plain_text
        assert "note-2" in rows[1].plain_text
        assert screen.query_one("#question-custom", Input) is not None
        assert screen.query_one("#question-hint", Static) is not None
        assert len(host.results) == 0


async def test_single_select_enter_accepts_first_option() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        await open_question(pilot, host, make_question())
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=("option-1",), custom=None)]
        assert len(host.screen_stack) == 1


async def test_single_select_down_then_enter_accepts_second_option() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question())
        await pilot.press("down")
        assert rows_of(screen)[1].has_class("q-highlighted")
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=("option-2",), custom=None)]


async def test_single_select_arrow_wraps_and_space_keeps_choice() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(options=3))
        await pilot.press("up")
        assert rows_of(screen)[2].has_class("q-highlighted")
        await pilot.press("space")
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=("option-3",), custom=None)]


async def test_click_selects_a_single_option_row() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(options=3))
        await pilot.click(rows_of(screen)[2])
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=("option-3",), custom=None)]


async def test_multi_select_empty_confirm_returns_no_selection() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        await open_question(pilot, host, make_question(multi_select=True))
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=(), custom=None)]


async def test_multi_select_space_toggles_options() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(options=3, multi_select=True))
        await pilot.press("space")
        await pilot.press("down")
        await pilot.press("space")
        assert rows_of(screen)[0].has_class("q-selected")
        assert rows_of(screen)[1].has_class("q-selected")
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=("option-1", "option-2"), custom=None)]


async def test_multi_select_space_twice_deselects() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        await open_question(pilot, host, make_question(multi_select=True))
        await pilot.press("space", "space")
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=(), custom=None)]


async def test_multi_select_click_toggles_row() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(options=2, multi_select=True))
        await pilot.click(rows_of(screen)[1])
        await pilot.pause()
        await pilot.click(rows_of(screen)[1])
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=(), custom=None)]


async def test_custom_answer_without_options() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(options=0))
        assert rows_of(screen) == []
        assert screen.query_one("#question-empty", Static) is not None
        await pilot.press("tab")
        assert isinstance(host.focused, Input)
        await pilot.press("h", "e", "l", "l", "o")
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=(), custom="hello")]


async def test_custom_answer_overrides_untouched_options() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        await open_question(pilot, host, make_question(options=2))
        await pilot.press("tab")
        assert isinstance(host.focused, Input)
        await pilot.press("c", "u", "s", "t", "o", "m")
        await pilot.press("enter")
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=(), custom="custom")]


async def test_tab_focuses_custom_input_then_buttons() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        await open_question(pilot, host, make_question(options=2))
        await pilot.press("tab")
        assert isinstance(host.focused, Input)
        await pilot.press("tab")
        assert isinstance(host.focused, Button)


async def test_escape_cancels_while_custom_input_is_focused() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        await open_question(pilot, host, make_question(options=2))
        await pilot.press("tab")
        assert isinstance(host.focused, Input)
        await pilot.press("x")
        assert host.focused.value == "x"
        await pilot.press("escape")
        await pilot.pause()
        assert host.results == [None]


async def test_skip_button_returns_empty_selection() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(options=2))
        screen.query_one("#" + SKIP_BUTTON_ID, Button).press()
        await pilot.pause()
        assert host.results == [Answer(id="q-1", selected=(), custom=None)]


async def test_cancel_button_returns_none() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question())
        screen.query_one("#" + CANCEL_BUTTON_ID, Button).press()
        await pilot.pause()
        assert host.results == [None]
        assert len(host.screen_stack) == 1


async def test_escape_cancels_the_question() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        await open_question(pilot, host, make_question())
        await pilot.press("escape")
        await pilot.pause()
        assert host.results == [None]


async def test_title_reports_position_for_multiple_questions() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(header="Scope"), index=1, total=3)
        title = screen.query_one("#question-title", Static)
        assert "Scope" in str(title.content)
        assert "2/3" in str(title.content)
        assert screen.index == 1
        assert screen.total == 3


async def test_initial_option_is_highlighted() -> None:
    host = QuestionHost()
    async with host.run_test(size=(100, 30)) as pilot:
        screen = await open_question(pilot, host, make_question(options=2))
        rows = rows_of(screen)
        assert rows[0].has_class("q-highlighted")
        assert not rows[1].has_class("q-highlighted")


async def test_screen_does_not_define_private_render_method() -> None:
    assert "_render" not in QuestionScreen.__dict__
    assert "_render" not in QuestionOptionRow.__dict__


class FakeQuestionService:
    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def configure(self, handler: Any) -> None:
        self.handlers.append(handler)


def _runtime_stub(service: Any) -> TuiRuntime:
    runtime = object.__new__(TuiRuntime)
    runtime.user_questions = service
    return runtime


async def test_runtime_configures_user_questions_service() -> None:
    service = FakeQuestionService()
    runtime = _runtime_stub(service)

    async def handler(request: AskRequest) -> list[Answer]:
        return []

    runtime.configure_user_questions(handler)
    assert service.handlers == [handler]
    runtime.configure_user_questions(None)
    assert service.handlers == [handler, None]


async def test_runtime_without_question_service_is_a_noop() -> None:
    runtime = _runtime_stub(None)

    async def handler(request: AskRequest) -> list[Answer]:
        return []

    runtime.configure_user_questions(handler)
    assert runtime.user_questions is None


async def test_app_ask_question_pages_through_every_question(monkeypatch: Any) -> None:
    app = DeepPyroApp()
    pushed: list[QuestionScreen] = []

    async def fake_push(screen: QuestionScreen) -> Answer | None:
        pushed.append(screen)
        return Answer(id=screen.question.id, selected=("option-1",))

    monkeypatch.setattr(app, "push_screen_wait", fake_push)
    first = make_question(question_id="q-1")
    second = make_question(question_id="q-2")
    answers = await app.ask_question(AskRequest(questions=(first, second), call_id="call-1"))
    assert answers == [
        Answer(id="q-1", selected=("option-1",)),
        Answer(id="q-2", selected=("option-1",)),
    ]
    assert [screen.question.id for screen in pushed] == ["q-1", "q-2"]
    assert [screen.index for screen in pushed] == [0, 1]
    assert [screen.total for screen in pushed] == [2, 2]


async def test_app_ask_question_stops_after_escape(monkeypatch: Any) -> None:
    app = DeepPyroApp()
    pushed: list[QuestionScreen] = []
    results: list[Answer | None] = [Answer(id="q-1", selected=("option-1",)), None]

    async def fake_push(screen: QuestionScreen) -> Answer | None:
        pushed.append(screen)
        return results[len(pushed) - 1]

    monkeypatch.setattr(app, "push_screen_wait", fake_push)
    request = AskRequest(
        questions=(make_question(question_id="q-1"), make_question(question_id="q-2")), call_id="c"
    )
    answers = await app.ask_question(request)
    assert answers == [Answer(id="q-1", selected=("option-1",))]
    assert len(pushed) == 2


async def test_app_ask_question_never_hangs_on_errors(monkeypatch: Any) -> None:
    app = DeepPyroApp()

    async def failing_push(screen: QuestionScreen) -> Answer | None:
        raise RuntimeError("screen crashed")

    monkeypatch.setattr(app, "push_screen_wait", failing_push)
    answers = await app.ask_question(AskRequest(questions=(make_question(),), call_id="c"))
    assert answers == []


async def test_app_ask_question_without_questions_returns_empty(monkeypatch: Any) -> None:
    app = DeepPyroApp()
    pushed: list[Any] = []

    async def fake_push(screen: Any) -> Any:
        pushed.append(screen)
        return None

    monkeypatch.setattr(app, "push_screen_wait", fake_push)
    answers = await app.ask_question(AskRequest(questions=(), call_id="c"))
    assert answers == []
    assert pushed == []


class QuestionAwareRuntime(FakeRuntime):
    def __init__(self, app: DeepPyroApp) -> None:
        super().__init__(app)
        self.question_handler: Any = None
        self.question_configures = 0

    def configure_user_questions(self, handler: Any) -> None:
        self.question_handler = handler
        self.question_configures += 1


async def test_app_wires_question_handler_after_boot() -> None:
    async def factory(app: DeepPyroApp) -> QuestionAwareRuntime:
        return QuestionAwareRuntime(app)

    app = DeepPyroApp(runtime_factory=factory, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        runtime = app.runtime
        assert isinstance(runtime, QuestionAwareRuntime)
        assert runtime.question_configures == 1
        assert runtime.question_handler == app.ask_question


async def test_app_ask_question_swallows_a_cancelled_wait(monkeypatch: Any) -> None:
    app = DeepPyroApp()
    pops: list[bool] = []

    async def cancelled_push(screen: QuestionScreen) -> Answer | None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(app, "push_screen_wait", cancelled_push)
    monkeypatch.setattr(app, "pop_screen", lambda: pops.append(True))
    answers = await app.ask_question(AskRequest(questions=(make_question(),), call_id="c"))
    assert answers == []
    assert pops == []


async def test_app_ask_question_pops_the_modal_after_a_cancel(monkeypatch: Any) -> None:
    app = DeepPyroApp(splash=False)

    async def cancelled_push(screen: QuestionScreen) -> Answer | None:
        app.push_screen(screen)
        await pilot.pause()
        raise asyncio.CancelledError()

    async with app.run_test(size=(80, 24)) as pilot:
        monkeypatch.setattr(app, "push_screen_wait", cancelled_push)
        answers = await app.ask_question(AskRequest(questions=(make_question(),), call_id="c"))
        await pilot.pause()
        assert answers == []
        assert isinstance(app.screen, QuestionScreen) is False
        assert len(app.screen_stack) == 1
