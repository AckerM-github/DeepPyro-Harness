"""Tests for the /hooks hook-decision viewer and its command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual.widgets import Static

from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.app_support import HOOK_TRACE_LIMIT
from deeppyro.tui.native_hook_trace import UNAVAILABLE_TEXT as HOOKS_UNAVAILABLE_TEXT
from deeppyro.tui.screens.hook_trace import (
    DETAIL_LIMIT,
    EMPTY_TEXT,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PENDING,
    HookTraceScreen,
    hook_detail,
    hook_duration,
    hook_mode,
    hook_name,
    hook_status,
    hook_trace_line,
)
from deeppyro.tui.widgets.prompt_area import PromptArea


def _text(widget: Static) -> str:
    content = widget.content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "hook": "tool.execute",
            "mode": "blocking",
            "invoked": {"hook": "tool.execute", "mode": "blocking"},
            "result": {
                "hook": "tool.execute",
                "mode": "blocking",
                "ok": True,
                "durationMs": 12,
                "preview": "read the file",
            },
        },
        {
            "hook": "llm.stream",
            "mode": "hot",
            "invoked": {"hook": "llm.stream", "mode": "hot"},
            "result": {
                "hook": "llm.stream",
                "mode": "hot",
                "ok": False,
                "outcome": "error",
                "durationMs": 5,
                "error": {"name": "ValueError", "message": "boom"},
            },
        },
        {
            "hook": "session.start",
            "mode": "blocking",
            "invoked": {"hook": "session.start", "mode": "blocking"},
            "result": None,
        },
    ]


def test_hook_trace_line_formats_status_duration_and_detail() -> None:
    assert hook_trace_line(_rows()[0]) == "tool.execute  blocking  ok  12ms  read the file"
    assert hook_trace_line(_rows()[1]) == "llm.stream  hot  error  5ms  ValueError: boom"
    assert hook_trace_line(_rows()[2]) == "session.start  blocking  pending"
    assert hook_status(_rows()[0]) == STATUS_OK
    assert hook_status(_rows()[1]) == STATUS_ERROR
    assert hook_status(_rows()[2]) == STATUS_PENDING


def test_hook_helpers_fall_back_to_the_invoked_record() -> None:
    row = {"invoked": {"hook": "cache.get", "mode": "hot"}}
    assert hook_name(row) == "cache.get"
    assert hook_mode(row) == "hot"
    assert hook_name({}) == "?"
    assert hook_mode({}) == "?"
    assert hook_duration({}) is None
    assert hook_status({}) == STATUS_PENDING


def test_hook_helpers_accept_flat_result_records() -> None:
    row = {"hook": "cache.set", "mode": "hot", "ok": False, "durationMs": 7, "preview": "p"}
    assert hook_status(row) == STATUS_ERROR
    assert hook_duration(row) == 7
    assert hook_detail(row) == "p"
    assert hook_trace_line(row) == "cache.set  hot  error  7ms  p"


def test_hook_detail_prefers_errors_and_clips_long_previews() -> None:
    error_row = {"result": {"ok": False, "error": {"name": "RuntimeError", "message": "bad"}}}
    assert hook_detail(error_row) == "RuntimeError: bad"
    assert hook_detail({"result": {"ok": False, "error": {"message": "only message"}}}) == "only message"
    long_row = {"result": {"ok": True, "preview": "x" * (DETAIL_LIMIT + 80)}}
    detail = hook_detail(long_row)
    assert len(detail) == DETAIL_LIMIT
    assert detail.endswith("…")
    multiline = {"result": {"ok": True, "preview": "line one" + chr(10) + "line two"}}
    assert hook_detail(multiline) == "line one line two"
    assert hook_detail({"result": {"ok": True, "preview": 123}}) == "123"
    assert hook_duration({"result": {"ok": True, "durationMs": "nan"}}) is None


class FakeRecorder:
    """Native hook recorder double: registry lines plus already-paired rows."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = list(rows or [])
        self.limits: list[int] = []

    def registry_lines(self) -> list[str]:
        return ["native hook registry: 3 hooks · 1 with listeners · 0 observed · 0 recent decision(s)"]

    def rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        self.limits.append(HOOK_TRACE_LIMIT if limit is None else limit)
        return [dict(row) for row in self._rows]


class FakeRuntime:
    def __init__(self, app: DeepPyroApp, recorder: Any) -> None:
        self.sink = app.sink
        self.cwd = Path("D:/work")
        self.provider_name = "fake"
        self.model = "fake-1"
        self.context_window = 8192
        self.hook_recorder: Any = recorder
        self.session: Any = None
        self.permission: Any = None
        self.agent_preset = "standard"
        self.agent_mode = "default"

    @property
    def session_id(self) -> str:
        return ""

    def session_usage(self) -> dict[str, int]:
        return {"input": 0, "output": 0, "cached": 0}

    async def close(self) -> None:
        return None


def _make_app(recorder: Any) -> DeepPyroApp:
    async def factory(app: DeepPyroApp) -> FakeRuntime:
        return FakeRuntime(app, recorder)

    return DeepPyroApp(runtime_factory=factory, splash=False)


def _system_texts(app: DeepPyroApp) -> list[str]:
    return [_text(widget) for widget in app.conversation.query(".system-block")]


async def test_hooks_command_without_a_recorder_reports_it() -> None:
    app = _make_app(None)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await app.hooks_command()
        assert any(HOOKS_UNAVAILABLE_TEXT in text for text in _system_texts(app))
        assert len(app.screen_stack) == 1


async def test_hooks_command_without_rows_reports_the_registry_and_empty_state() -> None:
    app = _make_app(FakeRecorder())
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await app.hooks_command()
        texts = _system_texts(app)
        assert any(text.startswith("native hook registry:") for text in texts)
        assert any(EMPTY_TEXT in text for text in texts)


async def test_hooks_command_opens_the_trace_screen_from_the_command_line() -> None:
    recorder = FakeRecorder(_rows())
    app = _make_app(recorder)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "/hooks"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, HookTraceScreen):
                break
        assert isinstance(app.screen, HookTraceScreen)
        assert app.screen.entries == _rows()
        assert recorder.limits == [HOOK_TRACE_LIMIT]
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_hooks_command_degrades_to_info_lines_outside_a_worker() -> None:
    recorder = FakeRecorder(_rows())
    app = _make_app(recorder)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        before = len(_system_texts(app))
        await app.hooks_command()
        texts = _system_texts(app)[before:]
        assert recorder.limits == [HOOK_TRACE_LIMIT]
        # one registry summary line plus the three paired rows
        assert len(texts) == 4
        assert texts[0].startswith("native hook registry:")
        assert texts[1].startswith("tool.execute")
        assert "ValueError: boom" in texts[2]
        assert "session.start" in texts[3]
