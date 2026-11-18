"""Tests for the native team manager path.

The lead session is a real native session under a temporary native root; child
sessions are created through the same control plane and driven by fake native
hosts. TeamService is a thin native-only alias of the manager.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.core.ids import now_ms
from deeppyro.observability.protocols import TurnUsage
from deeppyro.session.fold import fold
from deeppyro.session.native_control import NativeSessionControl, NewSession
from deeppyro.team.errors import NativeTeamError, TeamError
from deeppyro.team.manager import TeamService
from deeppyro.team.native_manager import NativeTeamManager
from deeppyro.team.types import (
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_RUNNING,
)


class FakeSessions:
    """Sessions service double: real native create/open, recorded calls."""

    def __init__(self, control: NativeSessionControl) -> None:
        self.control = control
        self.specs: list[NewSession] = []
        self.created: list[Any] = []
        self.opened: list[str] = []

    def native_create(self, spec: NewSession) -> Any:
        self.specs.append(spec)
        session = self.control.create(spec)
        self.created.append(session)
        return session

    def native_open(self, session_id: str) -> Any:
        self.opened.append(session_id)
        return self.control.open(session_id)


@dataclass
class FakeRunResult:
    final_text: str
    steps: int = 1


class FakeHost:
    """Native agent host double: no LLM, deterministic reply and usage counters."""

    def __init__(self, session: Any, build: Any) -> None:
        self.session = session
        self.build = build
        self.prompts: list[str] = []
        self.reply = "ok"
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.interrupted = False
        self.closed = False
        self.restored = 0
        self._input = 0
        self._output = 0
        self._cached = 0

    async def send(self, text: str) -> FakeRunResult:
        self.prompts.append(text)
        if self.gate is not None and len(self.prompts) == 1:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self._input += 10
        self._output += 5
        self._cached += 2
        service = self.build.services.get("usage")
        if service is not None:
            service.record_turn(
                TurnUsage(
                    provider=self.build.provider,
                    model=self.build.model,
                    session_id=self.session.header.id,
                    turn=len(self.prompts),
                    input_tokens=10,
                    output_tokens=5,
                    cached_tokens=2,
                )
            )
        return FakeRunResult(self.reply)

    def restore(self) -> bool:
        self.restored += 1
        return False

    def usage(self) -> dict[str, int]:
        return {"input": self._input, "output": self._output, "cached": self._cached}

    async def interrupt(self) -> bool:
        self.interrupted = True
        return True

    async def aclose(self) -> None:
        self.closed = True


class FakeAgents:
    """Agents service double that records hosts and builds."""

    def __init__(self, *, error: Exception | None = None, gate: asyncio.Event | None = None) -> None:
        self.error = error
        self.gate = gate
        self.hosts: list[FakeHost] = []
        self.builds: list[Any] = []

    def host(self, session: Any, build: Any) -> FakeHost:
        if self.error is not None:
            raise self.error
        host = FakeHost(session, build)
        host.gate = self.gate
        self.hosts.append(host)
        self.builds.append(build)
        return host


class FakeGate:
    def __init__(self, mode: str = "danger-full-access", approval: str = "never") -> None:
        self.mode = mode
        self.approval = approval


class FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def gate(self, workspace: Any, ask: Any = None, *, session: Any = None) -> FakeGate:
        self.calls.append({"workspace": Path(workspace), "ask": ask, "session": session})
        return FakeGate()


class FakeUsageSink:
    """Modern UsageSink double: one TurnUsage per finished child turn."""

    def __init__(self) -> None:
        self.turns: list[TurnUsage] = []

    def record_turn(self, usage: TurnUsage) -> TurnUsage:
        self.turns.append(usage)
        return usage


class LegacyUsageSink:
    """Legacy keyword sink double; the team adapter must keep it working."""

    def __init__(self) -> None:
        self.records: list[dict[str, int | str]] = []

    def record(self, **kwargs: Any) -> dict[str, Any]:
        self.records.append(dict(kwargs))
        return dict(kwargs)


@dataclass
class NativeTeamEnv:
    service: TeamService
    manager: NativeTeamManager
    parent: Any
    control: NativeSessionControl
    sessions: FakeSessions
    agents: FakeAgents
    sandbox: FakeSandbox
    sink: Any
    cwd: Path


def _configure(service: TeamService, *, cwd: Path, sink: Any = None) -> None:
    service.configure(
        cwd=cwd,
        provider=SimpleNamespace(name="local"),
        registry="registry",
        permission=FakeGate(),
        model="child-model",
        context_window=8192,
        preset="team",
        preset_revision=None,
        services={},
        ask=None,
        usage_sink=sink,
    )


def make_native_team(
    tmp_path: Path,
    *,
    agents: FakeAgents | None = None,
    sandbox: FakeSandbox | None = None,
    sink: Any = None,
    handle_parent: bool = False,
    configure: bool = True,
) -> NativeTeamEnv:
    control = NativeSessionControl(root=tmp_path / "sessions")
    parent = control.create(NewSession(cwd=tmp_path, preset="team"))
    sessions = FakeSessions(control)
    agents = agents or FakeAgents()
    sandbox = sandbox or FakeSandbox()
    service = TeamService(agents=agents, sessions=sessions, sandbox=sandbox)
    if configure:
        _configure(service, cwd=tmp_path, sink=sink)
    bound = NativeSessionHandle(parent) if handle_parent else parent
    service.bind_session(bound)
    manager = service
    assert isinstance(manager, NativeTeamManager)
    return NativeTeamEnv(
        service=service,
        manager=manager,
        parent=parent,
        control=control,
        sessions=sessions,
        agents=agents,
        sandbox=sandbox,
        sink=sink,
        cwd=tmp_path,
    )


async def shutdown(env: NativeTeamEnv) -> None:
    await env.service.close()
    if not env.parent.closed:
        env.parent.close()


def plugin_events(parent: Any) -> list[Any]:
    return list(fold(parent.header, parent.events).plugin_events)


async def spin(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


# -- spawn / roster ------------------------------------------------------
async def test_spawn_creates_native_child_session_and_member_facts(tmp_path: Path) -> None:
    env = make_native_team(tmp_path)
    try:
        info = env.service.spawn("scout", "look around", "hello")
        assert info.id.startswith("member-")
        assert info.id != info.session_id
        assert info.session_id
        spec = env.sessions.specs[-1]
        assert spec.origin == "subagent"
        assert spec.parent_id == env.parent.header.id
        assert spec.delegation_depth == 1
        assert spec.preset == "team"
        assert spec.cwd == tmp_path
        child = env.agents.hosts[-1].session
        assert child.header.id == info.session_id
        assert child.header.parent_id == env.parent.header.id
        member = await env.service.wait(info.id, timeout_s=5)
        assert member.phase == PHASE_COMPLETED
        assert member.last_reply == "ok"
        assert env.agents.hosts[-1].prompts == ["hello"]
        events = plugin_events(env.parent)
        changed = [event for event in events if event.type == "plugin.team.member.changed"]
        assert changed
        payload = changed[-1].data
        assert payload["member_id"] == info.id
        assert payload["session_id"] == info.session_id
        assert payload["phase"] == PHASE_COMPLETED
        assert payload["preset"] == "team"
        assert payload["team_id"] == env.parent.header.id
        # the host inherited the configured provider/model and no TUI hooks
        build = env.agents.builds[-1]
        assert build.provider == "local"
        assert build.model == "child-model"
        assert build.hooks is None
        assert build.preset == "team"
    finally:
        await shutdown(env)


async def test_native_binding_accepts_handle_and_task_board_round_trips(tmp_path: Path) -> None:
    env = make_native_team(tmp_path, handle_parent=True)
    try:
        info = env.service.spawn("scout", "", "hello")
        await env.service.wait(info.id, timeout_s=5)
        task = env.service.create_task("write docs", "spec section", write_scopes=("docs/spec/team.md",))
        assert task.id.startswith("task-")
        assert env.service.tasks() == [task]
        claimed = env.service.update_task(task.id, "claim", owner="scout")
        assert claimed.status == "in_progress"
        assert claimed.owner == "scout"
        assert claimed.revision == 2
        assert env.service.get_task(task.subject) is None
        assert env.service.get_task(task.id) == claimed
        facts = [event for event in plugin_events(env.parent) if event.type == "plugin.team.task"]
        assert facts
        assert facts[-1].data["task"]["status"] == "in_progress"
        assert facts[-1].data["team_id"] == env.parent.header.id
    finally:
        await shutdown(env)


async def test_spawn_rejects_duplicate_live_member_name(tmp_path: Path) -> None:
    env = make_native_team(tmp_path)
    try:
        env.service.spawn("scout", "", "hello")
        with pytest.raises(TeamError, match="already in use"):
            env.service.spawn("scout", "", "again")
    finally:
        await shutdown(env)


# -- messaging and queue -------------------------------------------------
async def test_send_while_running_queues_then_drains_after_turn(tmp_path: Path) -> None:
    gate = asyncio.Event()
    env = make_native_team(tmp_path, agents=FakeAgents(gate=gate))
    try:
        info = env.service.spawn("scout", "", "hello")
        host = env.agents.hosts[-1]
        while not host.prompts:
            await spin()
        assert env.service.send(info.id, "follow-up") == "queued"
        assert "follow-up" in env.manager._members[info.id].queue
        queued = [event for event in plugin_events(env.parent) if event.type == "plugin.team.message.queued"]
        assert [event.data["text"] for event in queued] == ["follow-up"]
        gate.set()
        member = await env.service.wait(info.id, timeout_s=5)
        assert member.phase == PHASE_IDLE
        assert host.prompts == ["hello", "follow-up"]
        delivered = [
            event for event in plugin_events(env.parent) if event.type == "plugin.team.message.delivered"
        ]
        assert [event.data["text"] for event in delivered] == ["follow-up"]
        assert member.last_reply == "ok"
    finally:
        await shutdown(env)


async def test_cancel_interrupts_host_and_drops_queued_messages(tmp_path: Path) -> None:
    gate = asyncio.Event()
    env = make_native_team(tmp_path, agents=FakeAgents(gate=gate))
    try:
        info = env.service.spawn("scout", "", "hello")
        host = env.agents.hosts[-1]
        while not host.prompts:
            await spin()
        assert env.service.send(info.id, "queued-work") == "queued"
        assert await env.service.cancel(info.id) is True
        assert host.interrupted is True
        member = env.service.get_member(info.id)
        assert member is not None
        assert member.phase == PHASE_CANCELLED
        assert env.manager._members[info.id].queue == []
        assert await env.service.cancel(info.id) is False
        with pytest.raises(TeamError, match="cannot receive messages"):
            env.service.send(info.id, "too late")
    finally:
        gate.set()
        await shutdown(env)


async def test_failed_run_records_member_error_and_rejects_new_messages(tmp_path: Path) -> None:
    env = make_native_team(tmp_path)
    try:
        info = env.service.spawn("scout", "", "hello")
        await env.service.wait(info.id, timeout_s=5)
        env.agents.hosts[-1].error = RuntimeError("provider down")
        assert env.service.send(info.id, "again") == "started"
        member = await env.service.wait(info.id, timeout_s=5)
        assert member.phase == PHASE_FAILED
        assert member.error == "RuntimeError: provider down"
        with pytest.raises(TeamError, match="cannot receive messages"):
            env.service.send(info.id, "more")
    finally:
        await shutdown(env)


# -- usage ---------------------------------------------------------------
async def test_turns_report_to_usage_sink_with_child_attribution(tmp_path: Path) -> None:
    sink = FakeUsageSink()
    env = make_native_team(tmp_path, sink=sink)
    try:
        info = env.service.spawn("scout", "", "hello")
        await env.service.wait(info.id, timeout_s=5)
        assert len(sink.turns) == 1
        turn = sink.turns[0]
        assert turn.session_id == info.session_id
        assert turn.source == "subagent"
        assert turn.input_tokens == 10
        runs = env.service.runs()
        assert len(runs) == 1
        assert runs[0].member_id == info.id
        assert runs[0].usage.input_tokens == 10
        assert runs[0].usage.total_tokens == 15
        assert env.service.usage().total_tokens == 15
        assert env.service.run(runs[0].run_id) == runs[0]
        # no usage fact is ever written to the parent session
        assert not [e for e in plugin_events(env.parent) if "usage" in e.type]
    finally:
        await shutdown(env)


async def test_legacy_keyword_usage_sink_still_receives_turns(tmp_path: Path) -> None:
    sink = LegacyUsageSink()
    env = make_native_team(tmp_path, sink=sink)
    try:
        info = env.service.spawn("scout", "", "hello")
        await env.service.wait(info.id, timeout_s=5)
        assert len(sink.records) == 1
        assert sink.records[0]["input_tokens"] == 10
        assert sink.records[0]["model"] == "child-model"
    finally:
        await shutdown(env)


# -- restore -------------------------------------------------------------
async def test_restore_rebuilds_roster_queue_and_board(tmp_path: Path) -> None:
    env = make_native_team(tmp_path)
    info = env.service.spawn("scout", "look around", "hello")
    await env.service.wait(info.id, timeout_s=5)
    task = env.service.create_task("write docs")
    env.parent.append(
        "plugin.team.message.queued",
        {
            "team_id": env.parent.header.id,
            "member_id": info.id,
            "text": "after restart",
            "created_at": now_ms(),
        },
    )
    await env.service.close()
    try:
        agents = FakeAgents()
        service = TeamService(agents=agents, sessions=env.sessions, sandbox=env.sandbox)
        _configure(service, cwd=tmp_path)
        service.bind_session(env.parent)
        roster = await service.restore()
        assert [member.id for member in roster] == [info.id]
        restored = roster[0]
        assert restored.phase in (PHASE_RUNNING, PHASE_IDLE)
        assert restored.session_id == info.session_id
        assert restored.name == "scout"
        assert service.get_task(task.id) is not None
        assert service.get_task(task.id).subject == "write docs"
        member = await service.wait(info.id, timeout_s=5)
        assert member.phase == PHASE_IDLE
        assert member.last_reply == "ok"
        assert agents.hosts[-1].prompts == ["after restart"]
        assert agents.hosts[-1].restored >= 1
        assert info.session_id in env.sessions.opened
        delivered = [
            event for event in plugin_events(env.parent) if event.type == "plugin.team.message.delivered"
        ]
        assert [event.data["text"] for event in delivered] == ["after restart"]
        await service.close()
    finally:
        if not env.parent.closed:
            env.parent.close()


async def test_restore_missing_child_session_marks_member_failed(tmp_path: Path) -> None:
    env = make_native_team(tmp_path)
    env.parent.append(
        "plugin.team.member.changed",
        {
            "team_id": env.parent.header.id,
            "member_id": "member-000000000000",
            "name": "ghost",
            "description": "",
            "provider": "local",
            "model": "child-model",
            "preset": "team",
            "session_id": "session-0000000000000000",
            "phase": "idle",
            "created_at": now_ms(),
        },
    )
    try:
        agents = FakeAgents()
        service = TeamService(agents=agents, sessions=env.sessions, sandbox=env.sandbox)
        _configure(service, cwd=tmp_path)
        service.bind_session(env.parent)
        roster = await service.restore()
        assert len(roster) == 1
        assert roster[0].phase == PHASE_FAILED
        assert roster[0].error is not None
        assert roster[0].error.startswith("cannot restore team member session:")
    finally:
        if not env.parent.closed:
            env.parent.close()


# -- error paths and contracts ------------------------------------------
async def test_spawn_requires_bound_parent_and_configuration(tmp_path: Path) -> None:
    control = NativeSessionControl(root=tmp_path / "sessions")
    parent = control.create(NewSession(cwd=tmp_path, preset="team"))
    try:
        service = TeamService(agents=FakeAgents(), sessions=FakeSessions(control), sandbox=FakeSandbox())
        with pytest.raises(TeamError, match="no bound parent session"):
            service.spawn("scout", "", "hello")
        service.bind_session(parent)
        with pytest.raises(TeamError, match="not configured"):
            service.spawn("scout", "", "hello")
    finally:
        parent.close()


async def test_spawn_cleans_up_child_when_host_build_fails(tmp_path: Path) -> None:
    agents = FakeAgents(error=RuntimeError("hosts unavailable"))
    env = make_native_team(tmp_path, agents=agents)
    try:
        with pytest.raises(TeamError, match="cannot create member agent: hosts unavailable"):
            env.service.spawn("scout", "", "hello")
        assert env.service.members() == []
        assert env.sessions.created[-1].closed is True
    finally:
        await shutdown(env)


async def test_native_services_without_control_plane_fail_typed(tmp_path: Path) -> None:
    control = NativeSessionControl(root=tmp_path / "sessions")
    parent = control.create(NewSession(cwd=tmp_path, preset="team"))
    try:
        service = TeamService(
            agents=SimpleNamespace(host=None),
            sessions=SimpleNamespace(native_create=None, native_open=None),
            sandbox=FakeSandbox(),
        )
        _configure(service, cwd=tmp_path)
        service.bind_session(parent)
        with pytest.raises(NativeTeamError, match="sessions service does not support native sessions"):
            service.spawn("scout", "", "hello")
    finally:
        parent.close()


async def test_restore_without_plugin_events_returns_empty_roster(tmp_path: Path) -> None:
    env = make_native_team(tmp_path)
    try:
        roster = await env.service.restore()
        assert roster == ()
    finally:
        await shutdown(env)


async def test_restore_from_handle_reads_the_folded_plugin_events(tmp_path: Path) -> None:
    env = make_native_team(tmp_path, handle_parent=True)
    info = env.service.spawn("scout", "", "hello")
    await env.service.wait(info.id, timeout_s=5)
    task = env.service.create_task("board item")
    await env.service.close()
    try:
        agents = FakeAgents()
        service = TeamService(agents=agents, sessions=env.sessions, sandbox=env.sandbox)
        _configure(service, cwd=tmp_path)
        service.bind_session(NativeSessionHandle(env.parent))
        roster = await service.restore()
        assert [member.id for member in roster] == [info.id]
        assert service.get_task(task.id) is not None
        await service.close()
    finally:
        if not env.parent.closed:
            env.parent.close()


def test_team_service_is_a_thin_native_alias() -> None:
    assert issubclass(TeamService, NativeTeamManager)
    service = TeamService()
    assert isinstance(service, NativeTeamManager)
    assert service.bound is False
    # Compatibility alias for call sites that still reach for the composed
    # backend; it must be this manager, never a second backend.
    assert service._native is service


def test_binding_a_non_native_session_raises_native_team_error(tmp_path: Path) -> None:
    service = TeamService(agents=FakeAgents(), sessions=SimpleNamespace(), sandbox=FakeSandbox())
    _configure(service, cwd=tmp_path)
    with pytest.raises(NativeTeamError, match="native parent session"):
        service.bind_session(SimpleNamespace(session_id="session-parent"))
    with pytest.raises(NativeTeamError, match="native parent session"):
        service.bind_parent(SimpleNamespace(session_id="session-parent"))


# -- the native module stays free of v4 session types --------------------
def test_native_manager_module_has_no_v4_session_imports() -> None:
    import deeppyro.team.native_manager as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    forbidden = (
        "from deeppyro.session.session",
        "from deeppyro.session.store",
        "from deeppyro.session.writer",
        "from deeppyro.core.wire",
        "SessionHeader",
        "LogRecord",
    )
    for needle in forbidden:
        assert needle not in source, f"native_manager must not depend on {needle!r}"
    assert "class NativeTeamManager" in source


def test_team_service_module_has_no_v4_session_imports() -> None:
    import deeppyro.team.manager as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    forbidden = (
        "from deeppyro.session.session",
        "from deeppyro.session.store",
        "from deeppyro.session.writer",
        "from deeppyro.core.wire",
        "SessionHeader",
        "LogRecord",
    )
    for needle in forbidden:
        assert needle not in source, f"TeamService must not depend on {needle!r}"
    assert "class TeamService(NativeTeamManager)" in source
