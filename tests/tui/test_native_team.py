"""Native TUI team mode: /team on|off, roster, deliveries and cold restore."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deeppyro.api import AGENT_MODE_TEAM, DEFAULT_PRESET_ID, TEAM_PRESET_ID
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.core.errors import DeeppyroError
from deeppyro.core.llm import Finish, StopReason, TextDelta
from deeppyro.tui.runtime import TuiRuntime
from deeppyro.tui.screens.team import TeamPanelScreen, delivery_lines
from tests.tui.test_native_runtime import _boot, _noop_title, _ScriptedProvider


def _plugin_events(runtime: TuiRuntime) -> list[Any]:
    return list(runtime.session.fold().plugin_events)


def _kinds(runtime: TuiRuntime) -> list[str]:
    return [str(event.type) for event in _plugin_events(runtime)]


async def _wait_running(manager: Any, member_id: str) -> None:
    for _ in range(500):
        member = manager._members.get(member_id)
        if member is not None and member.phase == "running" and member.host is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("team member did not start running")


async def test_native_team_toggle_binds_and_stops_the_native_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("member reply"), Finish(StopReason.STOP)]
    runtime, _events = await _boot(monkeypatch, tmp_path, provider, native=True)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        assert runtime.agent_mode != AGENT_MODE_TEAM
        assert await runtime.set_team_mode(True) == TEAM_PRESET_ID
        assert runtime.agent_mode == AGENT_MODE_TEAM
        manager = runtime.team._native
        assert manager is not None
        info = runtime.team.spawn("scout", "look around", "hello")
        member = await runtime.team.wait(info.id, timeout_s=5)
        assert member.phase == "completed"
        assert [item.id for item in runtime.team.members()] == [info.id]
        kinds = _kinds(runtime)
        assert "plugin.team.member.changed" in kinds
        # /team off stops the native manager and leaves the team facts untouched.
        before = len(runtime.session.events)
        assert await runtime.set_team_mode(False) == DEFAULT_PRESET_ID
        appended = [str(event.type) for event in runtime.session.events[before:]]
        assert "preset.selected" in appended
        assert not any(kind.startswith("plugin.team.") for kind in appended)
        assert manager.members() == []
        assert runtime.agent_mode != AGENT_MODE_TEAM
    finally:
        await runtime.close()


async def test_native_team_off_refuses_while_a_member_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    provider.gate = asyncio.Event()
    runtime, _events = await _boot(monkeypatch, tmp_path, provider, native=True)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        await runtime.set_team_mode(True)
        info = runtime.team.spawn("scout", "", "slow work")
        await _wait_running(runtime.team._native, info.id)
        with pytest.raises(DeeppyroError, match="cannot leave team mode while 1 member"):
            await runtime.set_team_mode(False)
        await runtime.team.cancel(info.id)
        provider.gate.set()
        assert await runtime.set_team_mode(False) == DEFAULT_PRESET_ID
    finally:
        provider.gate.set()
        await runtime.close()


async def test_native_team_deliveries_show_queued_and_delivered_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("first reply"), Finish(StopReason.STOP)]
    provider.gate = asyncio.Event()
    runtime, _events = await _boot(monkeypatch, tmp_path, provider, native=True)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        await runtime.set_team_mode(True)
        info = runtime.team.spawn("scout", "", "hello")
        await _wait_running(runtime.team._native, info.id)
        assert runtime.team.send(info.id, "follow-up") == "queued"
        queued = runtime.team.deliveries()[info.id]
        assert queued["queued"] == 1
        assert queued["delivered"] == 0
        lines = delivery_lines(runtime.team.members(), runtime.team.deliveries())
        assert lines[0].startswith("messages: 1 queued · 0 delivered")
        provider.gate.set()
        await runtime.team.wait(info.id, timeout_s=5)
        delivered = runtime.team.deliveries()[info.id]
        assert delivered["queued"] == 1
        assert delivered["delivered"] == 1
        assert delivered["last_state"] == "delivered"
        # the settled member report is routed through the parent host delivery.
        notices = [
            message.text()
            for message in runtime.session.derive_messages()
            if "team member scout" in message.text()
        ]
        assert notices
    finally:
        provider.gate.set()
        await runtime.close()


async def test_native_team_roster_restores_after_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _ScriptedProvider()
    provider.script = [TextDelta("done"), Finish(StopReason.STOP)]
    runtime, _events = await _boot(monkeypatch, tmp_path, provider, native=True)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    session_id = runtime.session_id
    member_ids: list[str] = []
    try:
        await runtime.set_team_mode(True)
        info = runtime.team.spawn("scout", "remember me", "hello")
        await runtime.team.wait(info.id, timeout_s=5)
        member_ids = [info.id]
        assert runtime.team.create_task("write docs", "spec").subject == "write docs"
    finally:
        await runtime.close()

    provider.script = [TextDelta("again"), Finish(StopReason.STOP)]
    resumed, _events = await _boot(monkeypatch, tmp_path, provider, native=True, resume=session_id)
    try:
        assert resumed.agent_mode == AGENT_MODE_TEAM
        members = resumed.team.members()
        assert [member.id for member in members] == member_ids
        assert members[0].phase in {"completed", "idle"}
        assert [task.subject for task in resumed.team.tasks()] == ["write docs"]
        sessions = resumed.kernel_app.require(SessionsPlugin)
        assert sessions.native_exists(members[0].session_id)
    finally:
        await resumed.close()


def test_team_panel_renders_deliveries_without_changing_the_v4_columns() -> None:
    members = [{"id": "m1", "name": "Alice", "phase": "idle"}]
    deliveries = {"m1": {"member_id": "m1", "queued": 2, "delivered": 1, "last": "queued now"}}
    screen = TeamPanelScreen(members, (), "team-1", deliveries=deliveries)
    assert screen.deliveries == deliveries
    lines = screen.delivery_lines
    assert lines[0] == "messages: 2 queued · 1 delivered"
    assert lines[1].startswith("  Alice: queued 2 · delivered 1 · last ")
    plain = TeamPanelScreen(members, (), "team-1")
    assert plain.deliveries == {}
    assert plain.delivery_lines == []
