"""Preset revision wiring in TuiRuntime (agent pinning and session records)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api import PluginDependencyError
from deeppyro.tui.runtime import TuiRuntime


class FakeSession:
    def __init__(self, preset: str = "default") -> None:
        self.header = SimpleNamespace(agentPreset=preset)
        self.records: list[Any] = []
        self.session_id = "session-test"

    def append(self, record_type: str, data: dict[str, Any]) -> None:
        self.records.append(SimpleNamespace(type=record_type, data=data))


class FakeStore:
    def __init__(self, opened: FakeSession) -> None:
        self.created: list[FakeSession] = []
        self.opened = opened

    def create(self, cwd: str, *, agent_preset: str | None = None) -> FakeSession:
        session = FakeSession(agent_preset or "default")
        self.created.append(session)
        return session

    def open(self, session_id: str) -> FakeSession:
        return self.opened

    def list(self) -> list[Any]:
        return []


class FakePresets:
    def __init__(self) -> None:
        self.ensured: list[tuple[str, int | None]] = []
        self.all_count = 0

    async def ensure(self, preset_id: str, *, revision: int | None = None) -> Any:
        self.ensured.append((preset_id, revision))
        return SimpleNamespace(id=preset_id, revision=revision or 0)

    async def ensure_all(self) -> None:
        self.all_count += 1


class FakeAgent:
    def __init__(self, revision: int | None) -> None:
        self.preset_revision = revision
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeAgents:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def options(self, **overrides: Any) -> Any:
        return SimpleNamespace(**overrides)

    def create(self, **kwargs: Any) -> FakeAgent:
        self.calls.append(kwargs)
        revision = kwargs.get("preset_revision")
        return FakeAgent(revision)


class FakeKernelApp:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(name)
        return self._services[name]


def make_runtime(*, store: FakeStore, presets: FakePresets, agents: FakeAgents) -> TuiRuntime:
    kernel = FakeKernelApp({"presets": presets, "tools": SimpleNamespace(registry_for=lambda cwd: object())})
    return TuiRuntime(
        cfg=SimpleNamespace(providers={}),
        cwd=Path("D:/work"),
        kernel_app=kernel,
        provider_name="fake",
        model="fake-1",
        context_window=8192,
        store=store,
        provider=object(),
        registry=object(),
        permission=SimpleNamespace(),
        agents=agents,
        sink=object(),
        ask=lambda request: None,
    )


@pytest.mark.asyncio
async def test_new_session_records_the_pinned_preset_revision() -> None:
    store = FakeStore(FakeSession())
    presets = FakePresets()
    agents = FakeAgents()
    runtime = make_runtime(store=store, presets=presets, agents=agents)

    await runtime.ensure_presets()
    session = await runtime.new_session()

    assert presets.all_count == 1
    revision = agents.calls[0]["preset_revision"]
    assert revision is None
    assert session.records[-1].type == "agent-preset/selected"
    assert session.records[-1].data == {"preset": "default"}


@pytest.mark.asyncio
async def test_open_session_pins_the_recorded_revision() -> None:
    opened = FakeSession("reviewer")
    opened.records = [
        SimpleNamespace(type="agent-preset/selected", data={"preset": "reviewer", "revision": 7})
    ]
    presets = FakePresets()
    agents = FakeAgents()

    def create(**kwargs: Any) -> FakeAgent:
        agents.calls.append(kwargs)
        return FakeAgent(7)

    agents.create = create  # type: ignore[method-assign]
    runtime = make_runtime(store=FakeStore(opened), presets=presets, agents=agents)

    session = await runtime.open_session("session-test")

    assert session is opened
    assert presets.ensured == [("reviewer", 7)]
    assert agents.calls[0]["preset_revision"] == 7
    assert runtime.preset_revision == 7
    assert runtime.agent_preset == "reviewer"


@pytest.mark.asyncio
async def test_open_session_falls_back_when_the_revision_is_gone() -> None:
    opened = FakeSession("reviewer")
    opened.records = [
        SimpleNamespace(type="agent-preset/selected", data={"preset": "reviewer", "revision": 7})
    ]
    presets = FakePresets()
    agents = FakeAgents()

    def create(**kwargs: Any) -> FakeAgent:
        if kwargs.get("preset_revision") is not None:
            raise PluginDependencyError("unknown revision")
        agents.calls.append(kwargs)
        return FakeAgent(11)

    agents.create = create  # type: ignore[method-assign]
    runtime = make_runtime(store=FakeStore(opened), presets=presets, agents=agents)

    await runtime.open_session("session-test")

    assert presets.ensured == [("reviewer", 7), ("reviewer", None)]
    assert agents.calls[0]["preset_revision"] is None
    assert runtime.preset_revision == 11


@pytest.mark.asyncio
async def test_set_agent_preset_materializes_and_records_the_latest_revision() -> None:
    session = FakeSession()
    presets = FakePresets()
    agents = FakeAgents()
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=presets, agents=agents)
    runtime.session = session
    previous = FakeAgent(0)
    runtime.agent = previous
    runtime.kernel_app._services["presets"] = presets

    class ResolvedPresets(FakePresets):
        def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> Any:
            return SimpleNamespace(id=preset_id or "default")

    service = ResolvedPresets()
    runtime.kernel_app._services["presets"] = service

    agents.create = lambda **kwargs: FakeAgent(11)  # type: ignore[method-assign]
    switched = await runtime.set_agent_preset("reviewer")

    assert switched == "reviewer"
    assert service.ensured == [("reviewer", None)]
    assert session.records[-1].type == "agent-preset/selected"
    assert session.records[-1].data == {"preset": "reviewer", "revision": 11}
    assert previous.closed is True
