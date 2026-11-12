"""Tests for the approval modal, risk badges and decision shortcuts."""

from __future__ import annotations

from typing import Any

from textual.app import App
from textual.widgets import Button, Static

from deeppyro.sandbox.types import ApprovalRequest
from deeppyro.tui.screens.approval import (
    ALLOW_ALWAYS,
    ALLOW_ONCE,
    DENY,
    DETAIL_LIMIT,
    RISK_DANGER,
    RISK_WARN,
    ApprovalScreen,
    dangerous_reason,
    risk_level,
)


class HostApp(App[None]):
    """Bare application used to open the modal and collect its result."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[str] = []


def _text(widget: Static) -> str:
    content: Any = widget.content
    plain = getattr(content, "plain", None)
    return str(plain if plain is not None else content)


def _request(**fields: Any) -> ApprovalRequest:
    return ApprovalRequest(**fields)


async def _open(app: HostApp, pilot: Any, request: Any) -> ApprovalScreen:
    app.push_screen(ApprovalScreen(request), callback=app.results.append)
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, ApprovalScreen)
    return screen


def test_decision_constants_are_stable() -> None:
    assert ALLOW_ONCE == "once"
    assert ALLOW_ALWAYS == "always"
    assert DENY == "deny"


def test_dangerous_reason_uses_the_sandbox_patterns() -> None:
    assert dangerous_reason("rm -rf build") == "recursive force delete"
    assert dangerous_reason("git push --force origin main") == "force push"
    assert dangerous_reason("echo hello") is None


def test_risk_level_classifies_requests() -> None:
    dangerous = _request(
        kind="command-approval",
        summary="Allow this shell command?",
        detail="rm -rf build",
    )
    assert risk_level(dangerous) == RISK_DANGER
    safe = _request(
        kind="command-approval",
        summary="Allow this shell command?",
        detail="echo hello",
    )
    assert risk_level(safe) == RISK_WARN
    escalation = _request(
        kind="sandbox-escalation",
        summary="Allow this command to run with danger-full-access for one call?",
        detail="needed for the build",
    )
    assert risk_level(escalation) == RISK_DANGER
    write = _request(
        kind="write-outside-workspace",
        summary="Allow this write outside the workspace?",
        detail="C:/tmp/out.txt",
    )
    assert risk_level(write) == RISK_WARN


async def test_dangerous_command_shows_a_red_badge() -> None:
    request = _request(
        kind="command-approval",
        summary="Allow this shell command?",
        detail="rm -rf build",
        tool="shell",
    )
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        badge = screen.query_one("#approval-risk", Static)
        assert screen.risk == RISK_DANGER
        assert "risk-danger" in badge.classes
        badge_text = _text(badge)
        assert badge_text.startswith("DANGER")
        assert "recursive force delete" in badge_text
        assert _text(screen.query_one("#approval-tool", Static)) == "Tool: shell"
        assert _text(screen.query_one("#approval-summary", Static)) == request.summary
        assert _text(screen.query_one("#approval-detail", Static)) == "rm -rf build"


async def test_safe_command_shows_a_warning_badge() -> None:
    request = _request(
        kind="command-approval",
        summary="Allow this shell command?",
        detail="echo hello",
    )
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        badge = screen.query_one("#approval-risk", Static)
        assert screen.risk == RISK_WARN
        assert "risk-warn" in badge.classes
        assert _text(badge) == "WARNING"
        assert _text(screen.query_one("#approval-tool", Static)) == "Tool: shell"


async def test_sandbox_escalation_to_full_access_is_danger() -> None:
    request = _request(
        kind="sandbox-escalation",
        summary="Allow this command to run with danger-full-access for one call?",
        detail="needed for the build",
    )
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        badge = screen.query_one("#approval-risk", Static)
        assert screen.risk == RISK_DANGER
        assert "danger-full-access" in _text(badge)
        assert _text(screen.query_one("#approval-tool", Static)) == "Tool: sandbox"


async def test_write_outside_workspace_falls_back_to_filesystem_tool() -> None:
    request = _request(
        kind="write-outside-workspace",
        summary="Allow this write outside the workspace?",
        detail="C:/tmp/out.txt",
    )
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        assert screen.risk == RISK_WARN
        assert _text(screen.query_one("#approval-tool", Static)) == "Tool: filesystem"


async def test_keyboard_shortcuts_return_decisions() -> None:
    cases = [
        ("y", ALLOW_ONCE),
        ("a", ALLOW_ALWAYS),
        ("n", DENY),
        ("escape", DENY),
    ]
    for key, expected in cases:
        app = HostApp()
        request = _request(kind="command-approval", summary="Allow this shell command?", detail="ls")
        async with app.run_test(size=(80, 24)) as pilot:
            await _open(app, pilot, request)
            await pilot.press(key)
            await pilot.pause()
            assert app.results == [expected]


async def test_allow_once_button_is_focused_and_tab_cycles() -> None:
    app = HostApp()
    request = _request(kind="command-approval", summary="Allow this shell command?", detail="ls")
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        assert screen.query_one("#allow-once", Button).has_focus
        await pilot.press("tab")
        await pilot.pause()
        assert screen.query_one("#allow-always", Button).has_focus
        await pilot.press("tab")
        await pilot.pause()
        assert screen.query_one("#deny", Button).has_focus
        await pilot.press("enter")
        await pilot.pause()
        assert app.results == [DENY]


async def test_button_press_returns_the_decision() -> None:
    app = HostApp()
    request = _request(kind="command-approval", summary="Allow this shell command?", detail="ls")
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        screen.query_one("#allow-always", Button).press()
        await pilot.pause()
        assert app.results == [ALLOW_ALWAYS]


async def test_long_detail_is_truncated() -> None:
    detail = "x" * (DETAIL_LIMIT + 120)
    request = _request(
        kind="command-approval",
        summary="Allow this shell command?",
        detail=detail,
    )
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        shown = _text(screen.query_one("#approval-detail", Static))
        assert len(shown) == DETAIL_LIMIT
        assert shown.endswith("…")


async def test_detail_is_optional() -> None:
    request = _request(kind="write-outside-workspace", summary="Allow this write?")
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        assert list(screen.query("#approval-detail")) == []
        assert screen.detail == ""


async def test_mapping_requests_are_supported() -> None:
    request = {
        "kind": "command-approval",
        "summary": "Allow this shell command?",
        "detail": "rm -rf build",
    }
    app = HostApp()
    async with app.run_test(size=(80, 24)) as pilot:
        screen = await _open(app, pilot, request)
        assert screen.risk == RISK_DANGER
        badge = screen.query_one("#approval-risk", Static)
        assert "recursive force delete" in _text(badge)
