"""Schedule delivery host used by the TUI runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deeppyro.config import Config
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


def make_config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "local-model"},
            "providers": {
                "local": {
                    "baseURL": "http://127.0.0.1:8000/v1",
                    "models": [{"id": "local-model", "contextWindow": 32768}],
                }
            },
        }
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


class FakeSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.followups: list[tuple[str, dict[str, Any]]] = []
        self.idle_waits = 0

    def followup(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        self.followups.append((text, dict(source or {})))

    async def wait_for_idle(self) -> None:
        self.idle_waits += 1


class FakeAgent:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeAgents:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.agents: list[FakeAgent] = []

    def options(self, **overrides: Any) -> dict[str, Any]:
        return dict(overrides)

    def create(self, **kwargs: Any) -> FakeAgent:
        agent = FakeAgent()
        self.created.append(kwargs)
        self.agents.append(agent)
        return agent


class FakeStore:
    def __init__(self, sessions: dict[str, FakeSession] | None = None) -> None:
        self.sessions = dict(sessions or {})
        self.created: list[dict[str, Any]] = []

    def open(self, session_id: str) -> FakeSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def create(self, cwd: str, **kwargs: Any) -> FakeSession:
        session = FakeSession(f"session-created-{len(self.created) + 1}")
        self.sessions[session.session_id] = session
        self.created.append({"cwd": cwd, **kwargs})
        return session


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


def make_runtime(
    *,
    session: FakeSession | None = None,
    store: FakeStore | None = None,
    agents: FakeAgents | None = None,
    schedule: FakeSchedule | None = None,
) -> TuiRuntime:
    live = session or FakeSession("session-live")
    runtime = TuiRuntime(
        cfg=make_config(),
        cwd=Path("D:/work"),
        kernel_app=None,
        provider_name="local",
        model="local-model",
        context_window=32768,
        store=store or FakeStore(),
        provider=None,
        registry=None,
        permission=None,
        agents=agents or FakeAgents(),
        sink=None,
        ask=None,
        schedule=schedule,
        session=live,
    )
    return runtime


async def test_deliver_to_current_session_shows_notice_and_follows_up() -> None:
    session = FakeSession("session-live")
    notices: list[str] = []
    runtime = make_runtime(session=session)
    runtime.configure_schedule_notice(notices.append)
    host = runtime_module._TuiScheduleHost(runtime)

    task = make_task(SessionTarget(session.session_id))
    receipt = await host.deliver(task, "Scheduled task: standup")

    assert notices == ["Scheduled task: standup"]
    assert session.followups == [
        (
            "Scheduled task: standup",
            {
                "kind": "schedule",
                "taskId": task.id,
                "occurrence": task.scheduled_at,
            },
        )
    ]
    assert receipt is not None
    assert receipt.target_session_id == session.session_id
    assert receipt.delivered_at.endswith("Z")


async def test_deliver_to_other_session_opens_it_with_a_silent_agent() -> None:
    other = FakeSession("session-other")
    agents = FakeAgents()
    runtime = make_runtime(store=FakeStore({other.session_id: other}), agents=agents)
    runtime.schedule_notice = lambda _text: None
    host = runtime_module._TuiScheduleHost(runtime)

    task = make_task(SessionTarget(other.session_id))
    receipt = await host.deliver(task, "Scheduled task: standup")
    await asyncio.sleep(0)

    assert receipt is not None
    assert receipt.target_session_id == other.session_id
    assert [entry[0] for entry in other.followups] == ["Scheduled task: standup"]
    assert len(agents.created) == 1
    assert agents.created[0]["session"] is other
    assert agents.created[0]["preset"] == runtime.agent_preset
    assert agents.agents[0].closed is True


async def test_new_once_target_reuses_its_session_on_later_deliveries() -> None:
    store = FakeStore()
    agents = FakeAgents()
    runtime = make_runtime(store=store, agents=agents)
    host = runtime_module._TuiScheduleHost(runtime)
    task = make_task(NewOnceTarget(create=SessionCreateSpec(cwd="D:/work", agent_preset="default")))

    first = await host.deliver(task, "first")
    second = await host.deliver(task, "second")
    await asyncio.sleep(0)

    assert first is not None and second is not None
    assert len(store.created) == 1
    session = store.open(first.target_session_id)
    assert [entry[0] for entry in session.followups] == ["first", "second"]
    assert second.target_session_id == first.target_session_id
    assert len(agents.created) == 2


async def test_new_target_creates_a_fresh_session_per_delivery() -> None:
    store = FakeStore()
    agents = FakeAgents()
    runtime = make_runtime(store=store, agents=agents)
    host = runtime_module._TuiScheduleHost(runtime)
    task = make_task(NewTarget(create=SessionCreateSpec(cwd="D:/work")))

    first = await host.deliver(task, "first")
    second = await host.deliver(task, "second")
    await asyncio.sleep(0)

    assert first is not None and second is not None
    assert first.target_session_id != second.target_session_id
    assert len(store.created) == 2


async def test_session_exists_checks_the_live_and_persisted_sessions() -> None:
    other = FakeSession("session-other")
    runtime = make_runtime(store=FakeStore({other.session_id: other}))
    host = runtime_module._TuiScheduleHost(runtime)

    assert await host.session_exists(runtime.session_id) is True
    assert await host.session_exists(other.session_id) is True
    assert await host.session_exists("") is False
    assert await host.session_exists("session-missing") is False
    assert await host.delegation_depth(runtime.session_id) == 0


async def test_start_and_stop_schedule_attach_the_host_and_timer() -> None:
    schedule = FakeSchedule()
    runtime = make_runtime(schedule=schedule)

    await runtime.start_schedule()
    assert isinstance(schedule.host, runtime_module._TuiScheduleHost)
    assert schedule.started == 1

    await runtime.stop_schedule()
    assert schedule.stopped == 1


async def test_start_schedule_without_service_is_a_noop() -> None:
    runtime = make_runtime()

    await runtime.start_schedule()
    await runtime.stop_schedule()
