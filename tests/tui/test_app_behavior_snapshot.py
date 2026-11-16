"""Behavior snapshots of the terminal app, taken before the app.py split.

These tests pin the observable behavior of DeepPyroApp through its public
surface: the widgets it composes, the transcript it renders, the status line it
refreshes and the approval decisions it returns. They are the baseline proof
for the module split: every later commit must keep them green without editing
them.

Only observable behavior is asserted here - widget presence, rendered text and
public method results - never private attributes or the module layout.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from textual.widgets import Static

from deeppyro.sandbox.types import ApprovalRequest
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.screens.approval import ApprovalScreen
from deeppyro.tui.widgets.activity_bar import ActivityBar
from deeppyro.tui.widgets.conversation import AssistantBlock, ConversationView
from deeppyro.tui.widgets.plugin_slots import PluginStatusBar
from deeppyro.tui.widgets.prompt_area import PromptArea
from deeppyro.tui.widgets.status_bar import StatusBar
from tests.tui.test_app import _info_texts, _make_runtime

WELCOME_LINES = [
    "DeepPyro Harness — python core, terminal UI",
    (
        "fake/fake-1  ·  agent preset Standard (standard · native presentation · default mode)  ·  "
        "/model and /provider switch the model; /preset switches the agent preset"
    ),
    "/permission and /cwd change the session settings",
    "Type a message. /help lists commands. Ctrl+C interrupts, Ctrl+D quits.",
]

IDLE_STATUS = "session 11112222 · fake/fake-1  ↑12 ↓3 · ctx 0% · workspace-write/ask · idle"

STATUS_COMMAND_LINES = [
    "session  session-11112222-3333-4444-5555-666677778888",
    "provider fake  (n/a)",
    "providers fake, fake-2",
    "model    fake/fake-1",
    "cwd      " + str(Path("D:/work")),
    "preset   Standard  (native · default mode)",
    "policy   workspace-write",
    "tokens   input 12  output 3  cached 0",
    "context  0% of 8192",
    "records  0  turns 0",
    "phase    idle",
]


def _plain(widget: Static) -> str:
    content: Any = getattr(widget, "content", "")
    return str(getattr(content, "plain", content))


def _status_line(app: DeepPyroApp) -> str:
    return _plain(app.query_one(StatusBar))


async def _wait_for_screen(app: DeepPyroApp, pilot: Any, screen_type: type, timeout: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.02)
        if isinstance(app.screen, screen_type):
            return app.screen
    raise AssertionError(f"{screen_type.__name__} did not appear within {timeout}s")


async def test_snapshot_boot_renders_the_main_screen() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert app.runtime is not None
        assert app.title == "DeepPyro Harness"
        assert app.sub_title == "fake/fake-1"
        for widget_type in (ActivityBar, ConversationView, StatusBar, PluginStatusBar, PromptArea):
            assert app.query_one(widget_type) is not None
        assert app.query_one(PromptArea).has_focus
        assert _info_texts(app) == WELCOME_LINES
        assert _status_line(app) == IDLE_STATUS
        assert app.busy is False


async def test_snapshot_a_user_turn_streams_the_reply() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        conversation = app.query_one(ConversationView)
        before = len(conversation.children)
        prompt = app.query_one(PromptArea)
        prompt.text = "hi there"
        await pilot.press("enter")
        await pilot.pause(0.3)

        # one user block, one agent process block, one assistant block
        assert len(conversation.children) == before + 3
        assert len(conversation.query(".user-block")) == 1
        assert _plain(conversation.query_one(".user-text", Static)) == "hi there"
        assert conversation.query_one(AssistantBlock).text == "hello world"
        assert app.sink.final_text == "hello world"
        assert app.busy is False
        assert _status_line(app) == IDLE_STATUS


async def test_snapshot_status_command_renders_the_session_summary() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        before = len(_info_texts(app))
        prompt = app.query_one(PromptArea)
        prompt.text = "/status"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert _info_texts(app)[before:] == STATUS_COMMAND_LINES
        assert app.busy is False
        assert _status_line(app) == IDLE_STATUS


async def test_snapshot_ctrl_c_clears_the_composer() -> None:
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        prompt = app.query_one(PromptArea)
        prompt.text = "draft text"
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert prompt.text == ""
        assert app.is_running is True
        assert app.busy is False


async def test_snapshot_approval_modal_accepts_and_rejects() -> None:
    request = ApprovalRequest(
        kind="command-approval",
        summary="Allow this shell command?",
        detail="rm -rf build",
        tool="shell",
    )
    app = DeepPyroApp(runtime_factory=_make_runtime, splash=False)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()

        allow_worker = app.run_worker(app.ask_approval(request), group="approval")
        screen = await _wait_for_screen(app, pilot, ApprovalScreen)
        assert _plain(screen.query_one("#approval-title", Static)) == "Approval required · shell command"
        assert _plain(screen.query_one("#approval-summary", Static)) == request.summary
        assert _plain(screen.query_one("#approval-detail", Static)) == "rm -rf build"
        await pilot.press("y")
        assert await asyncio.wait_for(allow_worker.wait(), timeout=5) is True

        deny_worker = app.run_worker(app.ask_approval(request), group="approval")
        await _wait_for_screen(app, pilot, ApprovalScreen)
        await pilot.press("n")
        assert await asyncio.wait_for(deny_worker.wait(), timeout=5) is False

    assert app.is_running is False
