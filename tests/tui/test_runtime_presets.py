"""Preset revision wiring in TuiRuntime (agent pinning and session records)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api import PluginDependencyError
from deeppyro.core.errors import DeeppyroError
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


class FakePreset:
    def __init__(
        self,
        preset_id: str,
        *,
        name: str = "",
        mode: str = "default",
        hidden: bool = False,
    ) -> None:
        self.id = preset_id
        self.name = name or preset_id
        self.description = ""
        self.mode = mode
        self.hidden = hidden


class FakePresets:
    def __init__(self, specs: list[FakePreset] | None = None) -> None:
        self.specs = specs or [
            FakePreset("default", name="Default"),
            FakePreset("reviewer", name="Reviewer"),
            FakePreset("team", name="Team", mode="team", hidden=True),
        ]
        self.ensured: list[tuple[str, int | None]] = []
        self.all_count = 0

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> FakePreset:
        wanted = preset_id or "default"
        for spec in self.specs:
            if spec.id == wanted:
                return spec
        raise ValueError(f"unknown agent preset {wanted!r}")

    def visible(self) -> list[FakePreset]:
        return [spec for spec in self.specs if not spec.hidden]

    def all(self) -> list[FakePreset]:
        return list(self.specs)

    async def ensure(self, preset_id: str, *, revision: int | None = None) -> Any:
        self.ensured.append((preset_id, revision))
        spec = self.resolve(preset_id)
        return SimpleNamespace(id=spec.id, revision=revision or 0, mode=spec.mode)

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
    assert session.records[-1].data == {"preset": "default", "mode": "default"}


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

    service = FakePresets()
    runtime.kernel_app._services["presets"] = service

    agents.create = lambda **kwargs: FakeAgent(11)  # type: ignore[method-assign]
    switched = await runtime.set_agent_preset("reviewer")

    assert switched == "reviewer"
    assert service.ensured == [("reviewer", None)]
    assert session.records[-1].type == "agent-preset/selected"
    assert session.records[-1].data == {"preset": "reviewer", "mode": "default", "revision": 11}
    assert previous.closed is True


@pytest.mark.asyncio
async def test_visible_presets_hide_team_from_the_picker() -> None:
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())

    assert [spec.id for spec in runtime.presets] == ["default", "reviewer"]


@pytest.mark.asyncio
async def test_set_agent_preset_rejects_the_hidden_team_preset() -> None:
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())

    with pytest.raises(DeeppyroError, match="/team on"):
        await runtime.set_agent_preset("team")


@pytest.mark.asyncio
async def test_team_mode_switches_and_records_the_previous_preset() -> None:
    session = FakeSession()
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())
    runtime.session = session
    previous = FakeAgent(0)
    runtime.agent = previous

    selected = await runtime.set_team_mode(True)

    assert selected == "team"
    assert runtime.agent_preset == "team"
    assert runtime.agent_mode == "team"
    assert runtime.team_previous_preset == "default"
    assert previous.closed is True
    assert session.records[-1].data == {"preset": "team", "mode": "team", "previous": "default"}

    restored = await runtime.set_team_mode(False)

    assert restored == "default"
    assert runtime.agent_preset == "default"
    assert runtime.agent_mode == "default"
    assert session.records[-1].data == {"preset": "default", "mode": "default"}


@pytest.mark.asyncio
async def test_team_mode_refuses_to_leave_with_live_members() -> None:
    class LiveTeam:
        def members(self) -> list[Any]:
            return [SimpleNamespace(name="scout", phase="running")]

    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())
    runtime.session = FakeSession()
    runtime.agent = FakeAgent(0)
    await runtime.set_team_mode(True)
    runtime.team = LiveTeam()

    with pytest.raises(DeeppyroError, match="1 member"):
        await runtime.set_team_mode(False)
    assert runtime.agent_preset == "team"


@pytest.mark.asyncio
async def test_open_session_restores_team_mode_and_its_previous_preset() -> None:
    opened = FakeSession()
    opened.records = [
        SimpleNamespace(
            type="agent-preset/selected",
            data={"preset": "team", "mode": "team", "previous": "reviewer"},
        )
    ]
    presets = FakePresets()
    runtime = make_runtime(store=FakeStore(opened), presets=presets, agents=FakeAgents())

    await runtime.open_session("session-test")

    assert presets.ensured == [("team", None)]
    assert runtime.agent_preset == "team"
    assert runtime.agent_mode == "team"
    assert runtime.team_previous_preset == "reviewer"


@pytest.mark.asyncio
async def test_switch_preset_rolls_back_when_agent_creation_fails() -> None:
    class FailingAgents(FakeAgents):
        def __init__(self) -> None:
            super().__init__()
            self.fail_after = 1

        def create(self, **kwargs: Any) -> FakeAgent:
            self.calls.append(kwargs)
            if len(self.calls) > self.fail_after:
                raise RuntimeError("agent creation failed")
            return FakeAgent(kwargs.get("preset_revision"))

    store = FakeStore(FakeSession())
    presets = FakePresets()
    agents = FailingAgents()
    runtime = make_runtime(store=store, presets=presets, agents=agents)
    await runtime.ensure_presets()
    await runtime.new_session()
    old_agent = runtime.agent
    records = len(runtime.session.records)

    with pytest.raises(RuntimeError, match="agent creation failed"):
        await runtime.set_agent_preset("reviewer")

    assert runtime.agent_preset == "default"
    assert runtime.preset_revision is None
    assert runtime.agent is old_agent
    assert old_agent is not None and not old_agent.closed
    assert len(runtime.session.records) == records


@pytest.mark.asyncio
async def test_team_off_restores_the_previous_custom_preset() -> None:
    session = FakeSession()
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())
    runtime.session = session
    runtime.agent = FakeAgent(0)
    runtime.agent_preset = "reviewer"

    await runtime.set_team_mode(True)

    assert runtime.team_previous_preset == "reviewer"
    assert session.records[-1].data == {"preset": "team", "mode": "team", "previous": "reviewer"}

    restored = await runtime.set_team_mode(False)

    assert restored == "reviewer"
    assert runtime.agent_preset == "reviewer"
    assert session.records[-1].data == {"preset": "reviewer", "mode": "default"}
