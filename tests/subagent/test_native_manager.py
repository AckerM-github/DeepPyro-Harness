"""Native subagent branch: spawn, prefix fork, restore, usage and notices.

These tests pin the native path in :mod:`deeppyro.subagent.native_manager` and
the routing in :class:`deeppyro.subagent.manager.SubagentService`.  They use real
native sessions on a temporary root and duck-typed agents/host fakes; the native
runtime is the only subagent backend.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import deeppyro.subagent.native_manager as native_manager
from deeppyro.api.types import DEFAULT_PRESET_ID
from deeppyro.core.messages import Message, PluginSource, TextPart, UserSource
from deeppyro.session.native_control import NativeSessionControl, NewSession
from deeppyro.session.schema import EventKind, NativeSession
from deeppyro.subagent import (
    NativeSubagentService,
    NativeSubagentServiceWorker,
    SubagentService,
)
from deeppyro.subagent.errors import SubagentError
from deeppyro.subagent.supervisor import SupervisorError, WorkerAssignment, WorkerBudget

RUN_ID = "run_" + "a" * 16
TURN_ID = "turn_" + "b" * 16
TRACE_ID = "c" * 32
USER_MESSAGE_ID = "msg_" + "1" * 16
CREATED_AT = 1_700_000_000_000

STARTED = "plugin.subagent.started"
FINISHED = "plugin.subagent.finished"
NOTICE = "plugin.subagent.notice"


# -- fakes -------------------------------------------------------------------


class FakeProvider:
    def __init__(self, name: str = "local") -> None:
        self.name = name


class FakeGate:
    def __init__(self, mode: str = "workspace-write", approval: str = "ask") -> None:
        self.policy = SimpleNamespace(mode=mode)
        self.approval = approval


class FakeLlm:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def provider(self, name: str) -> Any:
        self.calls.append(name)
        if name != "local":
            raise LookupError(f"unknown provider {name}")
        return FakeProvider(name)


class FakeSessions:
    """The native control plane a sessions plugin exposes."""

    def __init__(self, root: Path) -> None:
        self.native = NativeSessionControl(root=root)
        self.created: list[NewSession] = []
        self.opened: list[str] = []

    def native_create(self, spec: NewSession) -> NativeSession:
        self.created.append(spec)
        return self.native.create(spec)

    def native_open(self, session_id: str) -> NativeSession:
        self.opened.append(session_id)
        return self.native.open(session_id)


class FakeHost:
    """A child host: sends one prompt and reports provider usage counters."""

    def __init__(
        self,
        *,
        reply: str = "child reply",
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
        usage_map: dict[str, int] | None = None,
        steps: int = 2,
    ) -> None:
        self.reply = reply
        self.error = error
        self.gate = gate
        self.usage_map = dict(usage_map or {"input": 10, "output": 5, "cached": 2})
        self.steps = steps
        self.prompts: list[str] = []
        self.closed = False
        self.restored = 0

    async def send(self, text: str) -> Any:
        self.prompts.append(text)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(final_text=self.reply, steps=self.steps)

    def usage(self) -> dict[str, int]:
        return dict(self.usage_map)

    async def aclose(self) -> None:
        self.closed = True

    def restore(self) -> bool:
        self.restored += 1
        return True


class FakeAgents:
    """The agents plugin host() factory: records every AgentBuild."""

    def __init__(self, *, hosts: list[FakeHost] | None = None, error: Exception | None = None) -> None:
        self.hosts = list(hosts or [])
        self.error = error
        self.builds: list[Any] = []
        self.sessions: list[Any] = []
        self.host_objects: list[FakeHost] = []

    def host(self, session: Any, build: Any) -> FakeHost:
        self.builds.append(build)
        self.sessions.append(session)
        if self.error is not None:
            raise self.error
        host = self.hosts.pop(0) if self.hosts else FakeHost()
        self.host_objects.append(host)
        return host


class FakeParentHost:
    """The parent native host: owns the parent session and its notice queue."""

    def __init__(self, session: NativeSession, *, busy: bool = False) -> None:
        self.native_session = session
        self.busy = busy
        self.notices: list[tuple[str, Any]] = []
        self.deliveries = 0

    def deliver(self, text: str, *, source: Any = None) -> bool:
        self.deliveries += 1
        if self.busy:
            return True
        self.notices.append((text, source))
        return False


class RecordingUsageSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> None:
        self.records.append(
            {
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
            }
        )


# -- builders ----------------------------------------------------------------


def make_service(
    tmp_path: Path,
    *,
    agents: FakeAgents | None = None,
    max_runs: int = 4,
    usage_sink: Any = None,
    services: dict[str, Any] | None = None,
) -> tuple[NativeSubagentService, FakeSessions, FakeAgents, FakeGate]:
    sessions = FakeSessions(tmp_path)
    fake_agents = agents or FakeAgents()
    service = NativeSubagentService(agents=fake_agents, sessions=sessions, max_runs=max_runs)
    gate = FakeGate()
    service.configure(
        cwd=tmp_path,
        provider=FakeProvider(),
        registry=None,
        permission=gate,
        model="m",
        context_window=4096,
        services=services if services is not None else {"llm": FakeLlm(), "subagent": object()},
        usage_sink=usage_sink,
    )
    return service, sessions, fake_agents, gate


def bind_parent(
    service: NativeSubagentService,
    sessions: FakeSessions,
    tmp_path: Path,
    *,
    busy: bool = False,
) -> FakeParentHost:
    host = FakeParentHost(sessions.native_create(NewSession(cwd=tmp_path)), busy=busy)
    service.bind_session(host)
    service.bind_parent(host)
    return host


def facts(session: NativeSession, event_type: str) -> list[dict[str, Any]]:
    return [dict(event.data) for event in session.events if event.type == event_type]


def types(session: NativeSession) -> list[str]:
    return [event.type for event in session.events]


def write_parent_turn(parent: NativeSession) -> int:
    """Append one valid completed turn and a trailing plugin fact; return its last seq."""
    stamp = parent.last_ts + 1
    message = Message(
        id=USER_MESSAGE_ID,
        role="user",
        content=(TextPart("hello"),),
        created_at=stamp,
        source=UserSource(),
    )
    parent.append(EventKind.TURN_STARTED, {}, run_id=RUN_ID, turn_id=TURN_ID, trace_id=TRACE_ID)
    parent.append(EventKind.USER_MESSAGE, {"message": message.to_dict()}, turn_id=TURN_ID)
    parent.append(
        EventKind.TURN_ENDED,
        {"reason": {"kind": "completed"}},
        run_id=RUN_ID,
        turn_id=TURN_ID,
        trace_id=TRACE_ID,
    )
    parent.append("plugin.test.marker", {"still": "tail"})
    return next(event.seq for event in parent.events if event.type == EventKind.TURN_ENDED.value)


# -- spawn -------------------------------------------------------------------


async def test_spawn_creates_native_child_session_and_started_fact(tmp_path: Path) -> None:
    service, sessions, agents, gate = make_service(tmp_path)
    parent = bind_parent(service, sessions, tmp_path)

    result = await service.run("Summarize the file.", description="summarize")

    child = sessions.native.open(result.child_id)
    try:
        assert child.header.parent_id == parent.native_session.header.id
        assert child.header.origin == "subagent"
        assert child.header.delegation_depth == 1
        assert child.header.preset == DEFAULT_PRESET_ID
    finally:
        child.close()
    started = facts(parent.native_session, STARTED)
    assert len(started) == 1
    assert started[0]["version"] == 1
    assert started[0]["child_session_id"] == result.child_id
    assert started[0]["label"] == "subagent-1"
    assert started[0]["provider"] == "local"
    assert started[0]["model"] == "m"
    assert started[0]["mode"] == "spawn"
    assert started[0]["inherited_event_count"] == 0
    assert started[0]["created_at"] == int(child.header.created_at)

    build = agents.builds[0]
    assert build.provider == "local"
    assert build.model == "m"
    assert build.preset == DEFAULT_PRESET_ID
    assert build.cwd == tmp_path
    assert build.context_window == 4096
    assert build.permission is gate
    assert "subagent" not in build.services
    assert build.prompt_facts is not None
    await service.close()


async def test_run_returns_text_and_measured_usage(tmp_path: Path) -> None:
    sink = RecordingUsageSink()
    service, sessions, _, _ = make_service(tmp_path, usage_sink=sink)
    bind_parent(service, sessions, tmp_path)

    result = await service.run("count the files", description="count")

    assert result.text == "child reply"
    assert not result.is_error
    usage = result.usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (10, 5, 2)
    assert usage.total_tokens == 15
    assert usage.steps == 2
    assert usage.provider == "local"
    assert usage.model == "m"
    assert usage.latency_ms >= 0
    assert sink.records == [
        {
            "provider": "local",
            "model": "m",
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_tokens": 2,
        }
    ]
    assert service.usage(result.child_id) == usage
    assert service.usage() == usage
    await service.close()


async def test_finished_fact_keeps_usage_out_of_the_session_log(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    parent = bind_parent(service, sessions, tmp_path)

    result = await service.run("do the thing", description="thing")

    finished = facts(parent.native_session, FINISHED)
    assert len(finished) == 1
    assert finished[0]["child_session_id"] == result.child_id
    assert finished[0]["status"] == "ok"
    assert finished[0]["is_error"] is False
    assert finished[0]["usage_known"] is True
    assert finished[0]["text"] == "child reply"
    assert "input_tokens" not in finished[0]
    assert "usage" not in finished[0]
    assert "subagent/usage" not in types(parent.native_session)
    await service.close()


async def test_spawn_failure_reports_error_result_and_finished_fact(tmp_path: Path) -> None:
    agents = FakeAgents(hosts=[FakeHost(error=RuntimeError("boom"))])
    service, sessions, _, _ = make_service(tmp_path, agents=agents)
    parent = bind_parent(service, sessions, tmp_path)

    result = await service.run("fail", description="fail")

    assert result.is_error is True
    assert result.error == "RuntimeError: boom"
    finished = facts(parent.native_session, FINISHED)
    assert finished[0]["status"] == "error"
    assert finished[0]["is_error"] is True
    assert finished[0]["error"] == "RuntimeError: boom"
    await service.close()


async def test_spawn_rejects_unknown_provider_before_creating_a_child(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    bind_parent(service, sessions, tmp_path)

    with pytest.raises(SubagentError, match="unknown llm provider 'missing'"):
        await service.run("x", description="x", provider="missing")

    assert [spec for spec in sessions.created if spec.origin == "subagent"] == []
    await service.close()


# -- notices -----------------------------------------------------------------


async def test_start_delivers_settlement_notice_and_notice_fact(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    parent = bind_parent(service, sessions, tmp_path)

    activation = await service.start("check the tests", description="check")
    await service.wait_children()

    assert parent.deliveries == 1
    text, source = parent.notices[0]
    assert text.startswith(f"Background subagent {activation.child_id} finished")
    assert "will do no further work" in text
    assert text.endswith("Its closing message:\nchild reply")
    assert isinstance(source, PluginSource)
    assert source.plugin == "subagent"
    notice = facts(parent.native_session, NOTICE)
    assert len(notice) == 1
    assert notice[0]["child_session_id"] == activation.child_id
    assert notice[0]["state"] == "delivered"
    await service.close()


async def test_notice_is_queued_while_the_parent_is_busy(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    parent = bind_parent(service, sessions, tmp_path, busy=True)

    await service.start("check", description="check")
    await service.wait_children()

    assert parent.notices == []
    notice = facts(parent.native_session, NOTICE)
    assert notice[0]["state"] == "queued"
    await service.close()


async def test_failure_notice_says_the_child_failed(tmp_path: Path) -> None:
    agents = FakeAgents(hosts=[FakeHost(error=RuntimeError("boom"))])
    service, sessions, _, _ = make_service(tmp_path, agents=agents)
    parent = bind_parent(service, sessions, tmp_path)

    await service.start("fail", description="fail")
    await service.wait_children()

    text, _ = parent.notices[0]
    assert "failed before it finished" in text
    await service.close()


# -- fork prefix -------------------------------------------------------------


async def test_fork_copies_only_the_last_completed_turn_prefix(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    parent_session = sessions.native_create(NewSession(cwd=tmp_path))
    boundary = write_parent_turn(parent_session)
    parent = FakeParentHost(parent_session)
    service.bind_session(parent)

    activation = await service.start("continue", description="continue", mode="fork")
    await service.wait_children()

    child = sessions.native.open(activation.child_id)
    try:
        assert child.header.origin == "fork"
        assert child.header.parent_id == parent_session.header.id
        assert child.header.delegation_depth == parent_session.header.delegation_depth + 1
        assert len(child.events) == boundary
        assert types(child) == types(parent_session)[:boundary]
        for copied, source in zip(child.events, parent_session.events[:boundary], strict=True):
            assert copied.type == source.type
            assert copied.data == source.data
        assert types(child).count(EventKind.TURN_ENDED.value) == 1
    finally:
        child.close()
    started = facts(parent_session, STARTED)
    assert started[0]["mode"] == "fork"
    assert started[0]["inherited_event_count"] == boundary
    await service.close()


async def test_fork_rejects_provider_model_and_preset_overrides(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    parent_session = sessions.native_create(NewSession(cwd=tmp_path))
    write_parent_turn(parent_session)
    service.bind_session(FakeParentHost(parent_session))

    for override in ({"provider": "other"}, {"model": "other"}, {"preset": "other"}):
        with pytest.raises(SubagentError, match="fork does not support"):
            await service.start("x", description="x", mode="fork", **override)
    await service.close()


async def test_fork_falls_back_to_the_session_tail_without_a_completed_turn(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    parent_session = sessions.native_create(NewSession(cwd=tmp_path))
    parent_session.append("plugin.test.marker", {"still": "tail"})
    service.bind_session(FakeParentHost(parent_session))

    activation = await service.start("x", description="x", mode="fork")
    await service.wait_children()

    child = sessions.native.open(activation.child_id)
    try:
        assert len(child.events) == 1
        assert child.events[0].type == "plugin.test.marker"
    finally:
        child.close()
    await service.close()


# -- readiness, limits, cancellation ----------------------------------------


async def test_unconfigured_and_unbound_services_raise_typed_errors(tmp_path: Path) -> None:
    service = NativeSubagentService(agents=FakeAgents(), sessions=FakeSessions(tmp_path))
    with pytest.raises(SubagentError, match="not configured"):
        await service.run("x", description="x")

    service.configure(cwd=tmp_path, provider=FakeProvider(), model="m")
    with pytest.raises(SubagentError, match="no bound parent session"):
        await service.run("x", description="x")


async def test_closed_service_rejects_new_runs(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    bind_parent(service, sessions, tmp_path)
    await service.close()

    with pytest.raises(SubagentError, match="closed"):
        await service.run("x", description="x")


async def test_max_runs_rejects_a_second_live_child(tmp_path: Path) -> None:
    gate = asyncio.Event()
    service, sessions, _, _ = make_service(
        tmp_path, agents=FakeAgents(hosts=[FakeHost(gate=gate)]), max_runs=1
    )
    bind_parent(service, sessions, tmp_path)

    await service.start("first", description="first")
    with pytest.raises(SubagentError, match="at most 1 concurrent subagent runs"):
        await service.run("second", description="second")

    gate.set()
    await service.wait_children()
    await service.close()


async def test_cancel_all_settles_cancelled_children(tmp_path: Path) -> None:
    gate = asyncio.Event()
    agents = FakeAgents(hosts=[FakeHost(gate=gate), FakeHost(gate=gate)])
    service, sessions, _, _ = make_service(tmp_path, agents=agents)
    parent = bind_parent(service, sessions, tmp_path)

    await service.start("first", description="first")
    await service.start("second", description="second")
    assert service.live_runs == 2

    cancelled = await service.cancel_all()

    assert cancelled == 2
    assert service.live_runs == 0
    assert all(host.closed for host in agents.host_objects)
    finished = facts(parent.native_session, FINISHED)
    assert len(finished) == 2
    assert {item["status"] for item in finished} == {"cancelled"}
    await service.close()


# -- restore -----------------------------------------------------------------


async def test_restore_reopens_an_unfinished_child(tmp_path: Path) -> None:
    service, sessions, agents, _ = make_service(tmp_path)
    parent = sessions.native_create(NewSession(cwd=tmp_path))
    child = sessions.native_create(
        NewSession(cwd=tmp_path, origin="subagent", parent_id=parent.header.id, delegation_depth=1)
    )
    child_id = child.header.id
    child.close()
    parent.append(
        STARTED,
        {
            "version": 1,
            "child_session_id": child_id,
            "label": "scout",
            "provider": "local",
            "model": "m",
            "mode": "spawn",
            "inherited_event_count": 0,
            "created_at": CREATED_AT,
        },
    )
    service.bind_session(parent)

    restored = await service.restore()

    assert restored == (child_id,)
    assert sessions.opened == [child_id]
    assert agents.builds[0].provider == "local"
    assert agents.builds[0].model == "m"
    assert agents.host_objects[0].restored == 1
    assert service.parent_session_id == parent.header.id
    await service.close()


async def test_restore_skips_a_finished_child(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    parent = sessions.native_create(NewSession(cwd=tmp_path))
    child = sessions.native_create(
        NewSession(cwd=tmp_path, origin="subagent", parent_id=parent.header.id, delegation_depth=1)
    )
    child_id = child.header.id
    child.close()
    payload = {"version": 1, "child_session_id": child_id, "provider": "local", "model": "m", "mode": "spawn"}
    parent.append(STARTED, payload)
    parent.append(FINISHED, {**payload, "status": "ok", "is_error": False, "usage_known": True})
    service.bind_session(parent)

    assert await service.restore() == ()
    assert sessions.opened == []
    await service.close()


async def test_restore_without_a_parent_returns_empty(tmp_path: Path) -> None:
    service, _, _, _ = make_service(tmp_path)
    assert await service.restore() == ()


# -- manager routing and supervisor -----------------------------------------


async def test_manager_routes_a_native_parent_to_the_native_branch(tmp_path: Path) -> None:
    agents = FakeAgents()
    sessions = FakeSessions(tmp_path)
    service = SubagentService(agents=agents, sessions=sessions)
    service.configure(
        cwd=tmp_path,
        provider=FakeProvider(),
        registry=None,
        permission=FakeGate(),
        model="m",
        context_window=4096,
        services={"subagent": object()},
    )
    parent_session = sessions.native_create(NewSession(cwd=tmp_path))
    parent = FakeParentHost(parent_session)
    service.bind_session(parent)
    service.bind_parent(parent)

    assert service.parent_session_id == parent_session.header.id
    assert service.native.parent_session is parent_session
    result = await service.run("do it", description="do it")

    assert result.text == "child reply"
    assert service.native.usage(result.child_id) == result.usage
    assert facts(parent_session, STARTED)[0]["child_session_id"] == result.child_id
    await service.close()


async def test_manager_close_without_a_bound_parent_is_a_noop(tmp_path: Path) -> None:
    service = SubagentService(agents=FakeAgents(), sessions=FakeSessions(tmp_path))
    service.configure(
        cwd=tmp_path,
        provider=FakeProvider(),
        registry=None,
        permission=FakeGate(),
        model="m",
        context_window=4096,
    )
    service.bind_session(None)

    assert service.parent_session_id == ""
    assert service.live_runs == 0
    await service.close()


async def test_supervisor_native_worker_maps_the_subagent_result(tmp_path: Path) -> None:
    service, sessions, _, _ = make_service(tmp_path)
    bind_parent(service, sessions, tmp_path)
    worker = NativeSubagentServiceWorker(service)
    assignment = WorkerAssignment(
        worker_id="w1",
        objective="solve it",
        round=1,
        budget=WorkerBudget(max_steps=4),
    )

    outcome = await worker.run(assignment)

    assert outcome.summary == "child reply"
    assert outcome.done is True
    assert outcome.failed is False
    assert outcome.steps == 2
    assert outcome.tokens == 15
    assert outcome.usage is not None
    assert worker.service is service
    await service.close()


def test_supervisor_native_worker_requires_the_native_service() -> None:
    with pytest.raises(SupervisorError, match="requires a NativeSubagentService"):
        NativeSubagentServiceWorker(object())


# -- structural guards -------------------------------------------------------


def test_native_module_has_no_v4_imports() -> None:
    source = inspect.getsource(native_manager)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    banned = {
        "deeppyro.session.session",
        "deeppyro.session.writer",
        "deeppyro.session.fork",
        "deeppyro.session.layout",
        "deeppyro.core.wire",
        "deeppyro.agent.loop",
    }
    for module in imported:
        for prefix in banned:
            assert not (module == prefix or module.startswith(prefix + ".")), module
    for name in ("SessionHeader", "write_new_session", "build_fork_seed", "flat_session_path"):
        assert name not in source
    assert inspect.ismodule(native_manager)
    assert Path(native_manager.__file__).name == "native_manager.py"
