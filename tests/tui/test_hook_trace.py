"""Tests for the /hooks hook-decision viewer and its command."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from textual.app import App
from textual.widgets import Static

from deeppyro.tui.app import DeepPyroApp, _hook_rows_from_records, _normalize_hook_rows
from deeppyro.tui.screens.hook_trace import (
    DETAIL_LIMIT,
    EMPTY_TEXT,
    HINT_TEXT,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PENDING,
    TITLE,
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


def test_normalize_hook_rows_keeps_only_mappings() -> None:
    assert _normalize_hook_rows([{"hook": "a"}, None, "text"]) == [{"hook": "a"}]
    assert _normalize_hook_rows(None) == []
    assert _normalize_hook_rows({"hook": "a"}) == []


def test_record_pairing_matches_the_most_recent_invocation() -> None:
    records = [
        {"type": "hook/invoked", "data": {"hook": "a", "mode": "hot"}},
        SimpleNamespace(type="hook/invoked", data={"hook": "a", "mode": "hot"}),
        SimpleNamespace(type="hook/result", data={"hook": "a", "ok": True, "durationMs": 1}),
        SimpleNamespace(type="hook/result", data={"hook": "b", "ok": True}),
    ]
    rows = _hook_rows_from_records(SimpleNamespace(records=records))
    assert len(rows) == 2
    assert rows[0]["result"] is None
    assert rows[1]["result"] == {"hook": "a", "ok": True, "durationMs": 1}
    assert _hook_rows_from_records(SimpleNamespace(records=None)) == []


class ScreenHost(App[None]):
    def __init__(self, screen: HookTraceScreen) -> None:
        super().__init__()
        self._screen = screen
        self.results: list[Any] = []

    def on_mount(self) -> None:
        self.push_screen(self._screen, callback=self.results.append)


async def test_screen_renders_an_empty_state() -> None:
    screen = HookTraceScreen()
    host = ScreenHost(screen)
    async with host.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        assert screen.empty is True
        assert screen.rows == []
        assert _text(screen.query_one("#hooks-title", Static)) == TITLE
        assert _text(screen.query_one("#hooks-empty", Static)) == EMPTY_TEXT
        assert list(screen.query(".hook-row")) == []
        await pilot.press("escape")
        await pilot.pause()
        assert host.results == [None]
        assert len(host.screen_stack) == 1


async def test_screen_renders_every_paired_row() -> None:
    screen = HookTraceScreen([*_rows(), None])  # type: ignore[list-item]
    host = ScreenHost(screen)
    async with host.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        assert screen.empty is False
        assert screen.entries == _rows()
        rows = [_text(widget) for widget in screen.query(".hook-row")]
        assert rows == screen.rows
        assert rows[0].startswith("tool.execute")
        assert "ValueError: boom" in rows[1]
        assert _text(screen.query_one("#hooks-hint", Static)) == HINT_TEXT


def test_screen_does_not_define_a_private_render_method() -> None:
    assert "_render" not in HookTraceScreen.__dict__


class FakeSession:
    session_id = "session-hook-0001"

    def __init__(self) -> None:
        self.records: list[Any] = []

    def derive_messages(self) -> list[Any]:
        return []

    def usage(self) -> dict[str, int]:
        return {"input": 0, "output": 0, "cached": 0}

    def turn_count(self) -> int:
        return 0


class FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int]] = []

    def hook_trace(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self.calls.append((session_id, limit))
        return [dict(row) for row in self.rows]


class TraceSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.limits: list[int] = []

    async def hook_trace(self, limit: int = 50) -> list[dict[str, Any]]:
        self.limits.append(limit)
        return _rows()


class FakeRuntime:
    def __init__(self, app: DeepPyroApp) -> None:
        self.sink = app.sink
        self.cwd = Path("D:/work")
        self.provider_name = "fake"
        self.model = "fake-1"
        self.context_window = 8192
        self.session: FakeSession | None = FakeSession()
        self.session_query: Any = None
        self.kernel_app = None
        self.agent_preset = "standard"
        self.agent_mode = "default"

    @property
    def session_id(self) -> str:
        if self.session is None:
            return ""
        return str(getattr(self.session, "session_id", ""))

    def session_usage(self) -> dict[str, int]:
        return {"input": 0, "output": 0, "cached": 0}

    async def close(self) -> None:
        return None


async def _make_runtime(app: DeepPyroApp) -> FakeRuntime:
    return FakeRuntime(app)


def _system_texts(app: DeepPyroApp) -> list[str]:
    return [_text(widget) for widget in app.conversation.query(".system-block")]


async def test_hooks_command_without_hook_activity_reports_it() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        await app.hooks_command()
        assert any(EMPTY_TEXT in text for text in _system_texts(app))
        runtime = app.runtime
        assert isinstance(runtime, FakeRuntime)
        runtime.session = None
        await app.hooks_command()
        assert sum(EMPTY_TEXT in text for text in _system_texts(app)) == 2
        assert len(app.screen_stack) == 1


async def test_hooks_command_opens_the_trace_screen_from_the_command_line() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        runtime = app.runtime
        assert isinstance(runtime, FakeRuntime)
        query = FakeQuery(_rows())
        runtime.session_query = query
        prompt = app.query_one(PromptArea)
        prompt.text = "/hooks"
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause(0.05)
            if isinstance(app.screen, HookTraceScreen):
                break
        assert isinstance(app.screen, HookTraceScreen)
        assert query.calls == [(FakeSession.session_id, 50)]
        assert app.screen.entries == _rows()
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1


async def test_hooks_command_degrades_to_info_lines_outside_a_worker() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        runtime = app.runtime
        assert isinstance(runtime, FakeRuntime)
        session = TraceSession()
        runtime.session = session
        runtime.session_query = FakeQuery([])
        before = len(_system_texts(app))
        await app.hooks_command()
        texts = _system_texts(app)[before:]
        assert session.limits == [50]
        assert len(texts) == 3
        assert texts[0].startswith("tool.execute")
        assert "ValueError: boom" in texts[1]
        assert "session.start" in texts[2]


async def test_hooks_command_pairs_raw_session_records() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(110, 34)) as pilot:
        await pilot.pause()
        runtime = app.runtime
        assert isinstance(runtime, FakeRuntime)
        session = FakeSession()
        session.records = [
            SimpleNamespace(type="hook/invoked", data={"hook": "tool.execute", "mode": "blocking"}),
            SimpleNamespace(
                type="hook/result",
                data={"hook": "tool.execute", "mode": "blocking", "ok": True, "durationMs": 9},
            ),
            SimpleNamespace(type="hook/invoked", data={"hook": "llm.stream", "mode": "hot"}),
            SimpleNamespace(
                type="hook/result",
                data={
                    "hook": "llm.stream",
                    "mode": "hot",
                    "ok": False,
                    "durationMs": 2,
                    "error": {"name": "RuntimeError", "message": "bad"},
                },
            ),
        ]
        runtime.session = session
        before = len(_system_texts(app))
        await app.hooks_command()
        texts = _system_texts(app)[before:]
        assert any("tool.execute" in text and "9ms" in text for text in texts)
        assert any("RuntimeError: bad" in text for text in texts)
