"""Control bar option building and sync semantics."""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult

from deeppyro.tui.widgets.control_bar import (
    CUSTOM_WORKSPACE,
    CUSTOM_WORKSPACE_LABEL,
    MAX_WORKSPACE_OPTIONS,
    ControlBar,
    workspace_options,
)


def test_workspace_options_put_the_current_cwd_first() -> None:
    options = workspace_options("D:/work", ["D:/other", "D:/work"])
    assert options[0] == ("D:/work", "D:/work")
    assert [value for _, value in options][1] == "D:/other"
    assert options[-1] == (CUSTOM_WORKSPACE_LABEL, CUSTOM_WORKSPACE)


def test_workspace_options_dedupe_and_cap() -> None:
    workspaces = [f"D:/w{i}" for i in range(20)]
    options = workspace_options("D:/current", workspaces)
    values = [value for _, value in options]
    assert values[0] == "D:/current"
    assert len(values) == MAX_WORKSPACE_OPTIONS + 1
    assert len(set(values)) == len(values)


def test_workspace_options_tolerate_empty_values() -> None:
    options = workspace_options("", ["", "D:/real"])
    values = [value for _, value in options]
    assert values == ["D:/real", CUSTOM_WORKSPACE]


class ControlHost(App[Any]):
    def compose(self) -> ComposeResult:
        yield ControlBar()


async def test_control_bar_syncs_values() -> None:
    app = ControlHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        bar = app.query_one(ControlBar)
        bar.sync(
            preset="read-only",
            approval="never",
            cwd="D:/work",
            workspaces=["D:/other"],
        )
        await pilot.pause()
        assert bar.preset_select.value == "read-only"
        assert bar.approval_select.value == "never"
        assert bar.cwd_select.value == "D:/work"
        assert bar.cwd_values == ("D:/work", "D:/other", CUSTOM_WORKSPACE)


async def test_control_bar_sync_can_disable_selectors() -> None:
    app = ControlHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        bar = app.query_one(ControlBar)
        bar.sync(preset="workspace-write", approval="ask", cwd="D:/work", enabled=False)
        await pilot.pause()
        assert bar.preset_select.disabled is True
        assert bar.approval_select.disabled is True
        assert bar.cwd_select.disabled is True
        bar.sync(preset="workspace-write", approval="ask", cwd="D:/work", enabled=True)
        await pilot.pause()
        assert bar.preset_select.disabled is False


async def test_control_bar_ignores_invalid_sync_values() -> None:
    app = ControlHost()
    async with app.run_test(size=(120, 10)) as pilot:
        await pilot.pause()
        bar = app.query_one(ControlBar)
        bar.sync(preset="workspace-write", approval="ask", cwd="D:/work")
        bar.sync(preset="bogus", approval="bogus", cwd="D:/work")
        await pilot.pause()
        assert bar.preset_select.value == "workspace-write"
        assert bar.approval_select.value == "ask"
