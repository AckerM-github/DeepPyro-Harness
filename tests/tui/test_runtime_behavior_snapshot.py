"""Behavior snapshots for TuiRuntime, taken before the runtime.py split.

These tests pin the observable behavior of 'deeppyro.tui.runtime' as it stands
before the module is split into layered modules: construction and its dataclass
contract, session lifecycle and records, preset switching, kernel boot assembly,
and schedule delivery targets. They are the baseline proof for the split: every
later commit must keep them green without editing them.

Only observable behavior is asserted here - constructor fields/signature,
returned objects, session records and events, and results of public methods -
never the module layout. The fakes are local so the snapshot does not inherit
the assumptions of the per-area test modules.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api import DEFAULT_PRESET_ID
from deeppyro.config import Config
from deeppyro.core.errors import DeeppyroError
from deeppyro.hooks import SESSION_CREATED, SESSION_DISPOSED, HookService
from deeppyro.schedule import (
    AfterTiming,
    NewOnceTarget,
    NewTarget,
    ScheduleRecord,
    SessionCreateSpec,
    SessionTarget,
)
from deeppyro.tui import runtime as runtime_module
from deeppyro.tui.runtime import TuiRuntime
from tests.service_doubles import service_lookup, service_require

# -- fakes -------------------------------------------------------------


class FakeSession:
    def __init__(self, session_id: str = "session-live", preset: str = "standard") -> None:
        self.session_id = session_id
        self.header = SimpleNamespace(id=session_id, agentPreset=preset)
        self.records: list[SimpleNamespace] = []
        self.followups: list[tuple[str, dict[str, Any]]] = []
        self.idle_waits = 0
        self.usage_data = {"input": 10, "output": 5, "cached": 2}

    def append(self, record_type: str, data: dict[str, Any]) -> None:
        self.records.append(SimpleNamespace(type=record_type, data=dict(data)))

    def followup(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        self.followups.append((text, dict(source or {})))

    async def wait_for_idle(self) -> None:
        self.idle_waits += 1

    def usage(self) -> dict[str, int]:
        return dict(self.usage_data)


class FakeStore:
    def __init__(self, opened: FakeSession | None = None) -> None:
        self.created: list[FakeSession] = []
        self.create_calls: list[dict[str, Any]] = []
        self.opened_calls: list[str] = []
        self.opened = opened
        self.sessions: dict[str, FakeSession] = {}
        self.listed: list[Any] = []

    def create(self, cwd: str, *, agent_preset: str | None = None) -> FakeSession:
        session = FakeSession(f"session-{len(self.created) + 1}", agent_preset or "standard")
        self.created.append(session)
        self.create_calls.append({"cwd": cwd, "agent_preset": agent_preset})
        self.sessions[session.session_id] = session
        return session

    def open(self, session_id: str) -> FakeSession:
        self.opened_calls.append(session_id)
        if self.opened is not None and self.opened.session_id == session_id:
            return self.opened
        try:
            return self.sessions[session_id]
        except KeyError:
            raise KeyError(session_id) from None

    def list(self, *, include_archived: bool = False) -> list[Any]:
        return list(self.listed)


_UNSET = object()


class FakeAgent:
    def __init__(self, preset_revision: int | None = None, *, presentation: str = "native") -> None:
        self.preset_revision = preset_revision
        self.presentation = presentation
        self.busy = False
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeAgents:
    def __init__(self, forced_revision: Any = _UNSET) -> None:
        self.created: list[dict[str, Any]] = []
        self.agents: list[FakeAgent] = []
        self._forced_revision = forced_revision

    def options(self, **overrides: Any) -> Any:
        return SimpleNamespace(**overrides)

    def create(self, **kwargs: Any) -> FakeAgent:
        self.created.append(kwargs)
        revision = kwargs.get("preset_revision")
        if self._forced_revision is not _UNSET:
            revision = self._forced_revision
        agent = FakeAgent(revision)
        self.agents.append(agent)
        return agent


class FakePreset:
    def __init__(
        self,
        preset_id: str,
        *,
        name: str = "",
        mode: str = "default",
        hidden: bool = False,
        presentation: str = "native",
    ) -> None:
        self.id = preset_id
        self.name = name or preset_id
        self.mode = mode
        self.hidden = hidden
        self.presentation = presentation

    def display_name(self) -> str:
        return self.name or self.id


class FakePresets:
    def __init__(self) -> None:
        self.specs = [
            FakePreset("standard", name="Standard"),
            FakePreset("ptc", name="PTC", presentation="ptc"),
            FakePreset("team", name="Team", mode="team", hidden=True),
        ]
        self.ensured: list[tuple[str, int | None]] = []
        self.ensure_all_calls = 0

    def canonical(self, preset_id: str | None) -> str | None:
        value = str(preset_id or "").strip()
        if not value:
            return None
        return DEFAULT_PRESET_ID if value == "default" else value

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> FakePreset:
        wanted = self.canonical(preset_id) or DEFAULT_PRESET_ID
        for spec in self.specs:
            if spec.id == wanted:
                return spec
        raise ValueError(f"unknown agent preset {wanted!r}")

    def visible(self) -> list[FakePreset]:
        return [spec for spec in self.specs if not spec.hidden]

    def all(self) -> list[FakePreset]:
        return list(self.specs)

    async def ensure(self, preset_id: str, *, revision: int | None = None) -> None:
        self.ensured.append((preset_id, revision))

    async def ensure_all(self) -> None:
        self.ensure_all_calls += 1


class FakeKernelApp:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.stopped = 0

    def service(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError:
            raise LookupError(name) from None

    def get(self, service: type[Any]) -> Any | None:
        return service_lookup(self._services, service)

    def require(self, service: type[Any]) -> Any:
        return service_require(self._services, service)

    async def stop(self) -> None:
        self.stopped += 1


class FakeTools:
    def registry_for(self, workspace: Any, *, scope: str | None = None) -> Any:
        return SimpleNamespace(workspace=str(workspace), scope=scope)


class FakeSchedule:
    def __init__(self) -> None:
        self.host: Any = None
        self.started = 0
        self.stopped = 0

    def configure_host(self, host: Any) -> None:
        self.host = host

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


# -- helpers -----------------------------------------------------------


def make_config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "local-model"},
            "providers": {
                "local": {
                    "base_url": "http://127.0.0.1:8000/v1",
                    "models": [{"id": "local-model", "context_window": 32768}],
                }
            },
        }
    )


def make_runtime(
    *,
    store: FakeStore | None = None,
    agents: FakeAgents | None = None,
    presets: FakePresets | None = None,
    session: FakeSession | None = None,
    schedule: FakeSchedule | None = None,
    hooks: HookService | None = None,
) -> SimpleNamespace:
    presets = presets or FakePresets()
    hooks = hooks or HookService()
    kernel = FakeKernelApp({"presets": presets, "hooks": hooks, "tools": FakeTools()})
    runtime = TuiRuntime(
        cfg=make_config(),
        cwd=Path("D:/work"),
        kernel_app=kernel,
        provider_name="local",
        model="local-model",
        context_window=32768,
        store=store or FakeStore(),
        provider=object(),
        registry=object(),
        permission=SimpleNamespace(preset="workspace-write"),
        agents=agents or FakeAgents(),
        sink=object(),
        ask=lambda request: None,
        schedule=schedule,
        session=session,
    )
    return SimpleNamespace(
        runtime=runtime,
        kernel=kernel,
        store=store or runtime.store,
        agents=agents or runtime.agents,
        presets=presets,
        hooks=hooks,
    )


def make_task(target: Any, *, task_id: str = "task-1111") -> ScheduleRecord:
    return ScheduleRecord(
        id=task_id,
        title="standup",
        prompt="write the standup note",
        kind="after",
        timing=AfterTiming(after_seconds=60),
        scheduled_at="2026-11-14T03:00:00+08:00",
        status="active",
        target=target,
        created_at="2026-11-14T02:00:00+08:00",
        updated_at="2026-11-14T02:00:00+08:00",
    )


def records_of(session: FakeSession, record_type: str) -> list[dict[str, Any]]:
    return [record.data for record in session.records if record.type == record_type]


# -- construction ------------------------------------------------------


def test_constructor_exposes_selection_and_defaults() -> None:
    env = make_runtime()

    assert env.runtime.provider_name == "local"
    assert env.runtime.model == "local-model"
    assert env.runtime.context_window == 32768
    assert env.runtime.agent_preset == DEFAULT_PRESET_ID
    assert env.runtime.preset_revision is None
    assert env.runtime.session is None
    assert env.runtime.session_id == ""
    assert env.runtime.busy is False
    assert env.runtime.kernel_app is env.kernel
    assert env.runtime.cwd == Path("D:/work")


def test_constructor_is_a_dataclass_with_the_same_field_order() -> None:
    assert dataclasses.is_dataclass(TuiRuntime)
    names = [field.name for field in dataclasses.fields(TuiRuntime)]
    assert names[:13] == [
        "cfg",
        "cwd",
        "kernel_app",
        "provider_name",
        "model",
        "context_window",
        "store",
        "provider",
        "registry",
        "permission",
        "agents",
        "sink",
        "ask",
    ]
    assert names[-6:] == [
        "_catalog",
        "_schedule_tasks",
        "_schedule_once",
        "_created_sessions",
        "_restored_team_sessions",
        "_closed",
    ]
    parameters = list(inspect.signature(TuiRuntime).parameters)
    assert parameters[0:3] == ["cfg", "cwd", "kernel_app"]
    assert parameters[-6:] == names[-6:]
    assert parameters[13] == "activity"
    assert parameters[19] == "projections"
    assert parameters[23] == "agent_preset"


# -- sessions ----------------------------------------------------------


async def test_new_session_creates_durable_session_and_attaches_agent() -> None:
    created_events: list[dict[str, Any]] = []
    hooks = HookService()
    hooks.on(SESSION_CREATED, lambda payload: created_events.append(dict(payload)))
    env = make_runtime(hooks=hooks)

    session = await env.runtime.new_session()

    assert session is env.store.created[0]
    assert env.runtime.session is session
    assert env.store.create_calls == [{"cwd": str(env.runtime.cwd), "agent_preset": "standard"}]
    assert env.agents.created[-1]["session"] is session
    assert env.agents.created[-1]["preset"] == "standard"
    assert created_events == [
        {"sessionId": session.session_id, "cwd": str(env.runtime.cwd), "preset": "standard"}
    ]
    assert records_of(session, "agent-preset/selected")[-1] == {
        "preset": "standard",
        "mode": "default",
    }
    assert records_of(session, "model/selection")[-1] == {
        "provider": "local",
        "model": "local-model",
    }


async def test_open_session_restores_the_recorded_preset_revision() -> None:
    opened = FakeSession("session-9", preset="default")
    opened.records.append(
        SimpleNamespace(
            type="agent-preset/selected",
            data={"preset": "ptc", "revision": 7, "mode": "default"},
        )
    )
    store = FakeStore(opened=opened)
    env = make_runtime(store=store)

    session = await env.runtime.open_session("session-9")

    assert session is opened
    assert env.runtime.session is opened
    assert store.opened_calls == ["session-9"]
    assert env.runtime.agent_preset == "ptc"
    assert env.runtime.preset_revision == 7
    assert env.presets.ensured == [("ptc", 7)]
    assert env.agents.created[-1]["preset"] == "ptc"
    assert env.agents.created[-1]["preset_revision"] == 7


async def test_open_session_falls_back_to_the_header_preset() -> None:
    opened = FakeSession("session-9", preset="ptc")
    env = make_runtime(store=FakeStore(opened=opened))

    await env.runtime.open_session("session-9")

    assert env.runtime.agent_preset == "ptc"
    assert env.runtime.preset_revision is None
    assert env.presets.ensured == [("ptc", None)]
    assert env.agents.created[-1]["preset"] == "ptc"


async def test_close_disposes_created_sessions_once() -> None:
    disposed_events: list[dict[str, Any]] = []
    hooks = HookService()
    hooks.on(SESSION_DISPOSED, lambda payload: disposed_events.append(dict(payload)))
    env = make_runtime(hooks=hooks)
    first = await env.runtime.new_session()
    second = await env.runtime.new_session()

    await env.runtime.close()

    assert [event["sessionId"] for event in disposed_events] == ["session-1", "session-2"]
    assert env.kernel.stopped == 1
    assert env.runtime.kernel_app is None

    await env.runtime.close()
    assert len(disposed_events) == 2
    assert env.kernel.stopped == 1
    assert first.session_id == "session-1" and second.session_id == "session-2"


# -- presets -----------------------------------------------------------


async def test_set_agent_preset_switches_and_records_the_latest_revision() -> None:
    agents = FakeAgents(forced_revision=11)
    env = make_runtime(agents=agents)
    session = await env.runtime.new_session()

    switched = await env.runtime.set_agent_preset("ptc")

    assert switched == "ptc"
    assert env.runtime.agent_preset == "ptc"
    assert env.runtime.preset_revision == 11
    assert env.agents.created[-1]["preset"] == "ptc"
    assert records_of(session, "agent-preset/selected")[-1] == {
        "preset": "ptc",
        "mode": "default",
        "revision": 11,
    }

    canonical = await env.runtime.set_agent_preset("default")
    assert canonical == "standard"
    assert env.runtime.agent_preset == "standard"
    assert records_of(session, "agent-preset/selected")[-1] == {
        "preset": "standard",
        "mode": "default",
        "revision": 11,
    }


async def test_set_agent_preset_rejects_the_hidden_team_preset() -> None:
    env = make_runtime()
    await env.runtime.new_session()

    with pytest.raises(DeeppyroError, match="/team on"):
        await env.runtime.set_agent_preset("team")


async def test_set_team_mode_enters_and_leaves_with_records() -> None:
    env = make_runtime()
    session = await env.runtime.new_session()

    entered = await env.runtime.set_team_mode(True)
    assert entered == "team"
    assert env.runtime.agent_mode == "team"
    assert env.runtime.team_previous_preset == "standard"
    assert records_of(session, "agent-preset/selected")[-1] == {
        "preset": "team",
        "mode": "team",
        "previous": "standard",
    }

    left = await env.runtime.set_team_mode(False)
    assert left == "standard"
    assert env.runtime.agent_mode == "default"
    assert records_of(session, "agent-preset/selected")[-1] == {
        "preset": "standard",
        "mode": "default",
    }


# -- boot --------------------------------------------------------------


async def test_boot_assembles_the_runtime_from_kernel_services(monkeypatch: Any, tmp_path: Path) -> None:
    store = FakeStore()
    presets = FakePresets()
    agents = FakeAgents()
    tools = FakeTools()
    kernel = FakeKernelApp(
        {
            "llm": SimpleNamespace(provider=lambda name: SimpleNamespace(providerName=name)),
            "sessions": SimpleNamespace(store=store),
            "sandbox": SimpleNamespace(gate=lambda workspace, ask: SimpleNamespace(workspace=workspace)),
            "tools": tools,
            "agents": agents,
            "presets": presets,
        }
    )
    boot_calls: list[Any] = []

    async def fake_boot(cfg: Any) -> FakeKernelApp:
        boot_calls.append(cfg)
        return kernel

    monkeypatch.setattr("deeppyro.kernel.boot", fake_boot)
    config = make_config()

    runtime = await TuiRuntime.boot(sink=object(), ask=lambda request: None, cwd=str(tmp_path), config=config)

    assert boot_calls == [config]
    assert runtime.kernel_app is kernel
    assert runtime.provider_name == "local"
    assert runtime.model == "local-model"
    assert runtime.context_window == 32768
    assert runtime.provider.providerName == "local"
    assert runtime.registry.workspace == str(runtime.cwd)
    assert runtime.store is store
    assert runtime.agents is agents
    assert presets.ensure_all_calls == 1
    assert [spec.id for spec in runtime.presets] == ["standard", "ptc"]
    assert len(store.created) == 1
    assert store.created[0] is runtime.session


async def test_boot_stops_the_kernel_when_service_assembly_fails(monkeypatch: Any, tmp_path: Path) -> None:
    kernel = FakeKernelApp(
        {
            "llm": SimpleNamespace(provider=lambda name: (_ for _ in ()).throw(RuntimeError("boom"))),
            "sessions": SimpleNamespace(store=FakeStore()),
            "sandbox": SimpleNamespace(gate=lambda workspace, ask: object()),
            "tools": FakeTools(),
            "agents": FakeAgents(),
        }
    )

    async def fake_boot(cfg: Any) -> FakeKernelApp:
        return kernel

    monkeypatch.setattr("deeppyro.kernel.boot", fake_boot)

    with pytest.raises(RuntimeError, match="boom"):
        await TuiRuntime.boot(
            sink=object(), ask=lambda request: None, cwd=str(tmp_path), config=make_config()
        )

    assert kernel.stopped == 1


# -- schedule delivery -------------------------------------------------


async def test_schedule_host_delivers_to_the_live_session() -> None:
    session = FakeSession("session-live")
    notices: list[str] = []
    env = make_runtime(session=session)
    env.runtime.configure_schedule_notice(notices.append)
    host = runtime_module._TuiScheduleHost(env.runtime)
    task = make_task(SessionTarget(session.session_id))

    receipt = await host.deliver(task, "Scheduled task: standup")

    assert notices == ["Scheduled task: standup"]
    assert session.followups == [
        (
            "Scheduled task: standup",
            {"kind": "schedule", "taskId": task.id, "occurrence": task.scheduled_at},
        )
    ]
    assert receipt is not None
    assert receipt.target_session_id == session.session_id
    assert receipt.delivered_at.endswith("Z")


async def test_schedule_host_new_once_reuses_its_session() -> None:
    store = FakeStore()
    agents = FakeAgents()
    env = make_runtime(store=store, agents=agents)
    host = runtime_module._TuiScheduleHost(env.runtime)
    task = make_task(NewOnceTarget(create=SessionCreateSpec(cwd="D:/work", agent_preset="standard")))

    first = await host.deliver(task, "first")
    second = await host.deliver(task, "second")
    await asyncio.sleep(0)

    assert first is not None and second is not None
    assert len(store.created) == 1
    assert second.target_session_id == first.target_session_id
    assert [text for text, _ in store.created[0].followups] == ["first", "second"]
    assert len(agents.created) == 2
    assert all(agent.closed for agent in agents.agents)


async def test_schedule_host_new_target_creates_a_fresh_session_per_delivery() -> None:
    store = FakeStore()
    env = make_runtime(store=store)
    host = runtime_module._TuiScheduleHost(env.runtime)
    task = make_task(NewTarget(create=SessionCreateSpec(cwd="D:/work")))

    first = await host.deliver(task, "first")
    second = await host.deliver(task, "second")
    await asyncio.sleep(0)

    assert first is not None and second is not None
    assert first.target_session_id != second.target_session_id
    assert len(store.created) == 2


async def test_schedule_host_reports_session_existence() -> None:
    other = FakeSession("session-other")
    env = make_runtime(store=FakeStore(opened=other), session=FakeSession("session-live"))
    host = runtime_module._TuiScheduleHost(env.runtime)

    assert await host.session_exists("session-live") is True
    assert await host.session_exists("session-other") is True
    assert await host.session_exists("") is False
    assert await host.session_exists("session-missing") is False
    assert await host.delegation_depth("session-live") == 0
