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
    def __init__(self, preset: str = "standard") -> None:
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
        session = FakeSession(agent_preset or "standard")
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
        description: str = "",
        mode: str = "default",
        hidden: bool = False,
        presentation: str = "native",
    ) -> None:
        self.id = preset_id
        self.name = name or preset_id
        self.description = description
        self.mode = mode
        self.hidden = hidden
        self.presentation = presentation

    def display_name(self) -> str:
        return self.name or self.id


class FakePresets:
    def __init__(self, specs: list[FakePreset] | None = None) -> None:
        self.specs = specs or [
            FakePreset("standard", name="Standard", description="host-wide tools"),
            FakePreset("ptc", name="PTC", description="run_code-only", presentation="ptc"),
            FakePreset("minimal", name="Minimal", description="small surface"),
            FakePreset("cordis", name="Cordis", description="cordis prompt"),
            FakePreset("team", name="Team", mode="team", hidden=True),
        ]
        self.ensured: list[tuple[str, int | None]] = []
        self.all_count = 0

    def canonical(self, preset_id: str | None) -> str | None:
        value = str(preset_id or "").strip()
        if not value:
            return None
        return "standard" if value == "default" else value

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> FakePreset:
        wanted = self.canonical(preset_id) or "standard"
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
    def __init__(self, revision: int | None, *, presentation: str = "native") -> None:
        self.preset_revision = revision
        self.presentation = presentation
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
        return FakeAgent(revision, presentation=_presentation_for(kwargs.get("preset")))


def _presentation_for(preset: Any) -> str:
    return "ptc" if str(preset or "") == "ptc" else "native"


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
    assert agents.calls[0]["preset"] == "standard"
    assert runtime.agent_preset == "standard"
    assert runtime.agent_preset_display == "Standard"
    assert runtime.agent_presentation == "native"
    assert session.header.agentPreset == "standard"
    assert session.records[-1].type == "agent-preset/selected"
    assert session.records[-1].data == {"preset": "standard", "mode": "default"}


@pytest.mark.asyncio
async def test_open_session_pins_the_recorded_revision() -> None:
    opened = FakeSession("ptc")
    opened.records = [SimpleNamespace(type="agent-preset/selected", data={"preset": "ptc", "revision": 7})]
    presets = FakePresets()
    agents = FakeAgents()

    def create(**kwargs: Any) -> FakeAgent:
        agents.calls.append(kwargs)
        return FakeAgent(7, presentation="ptc")

    agents.create = create  # type: ignore[method-assign]
    runtime = make_runtime(store=FakeStore(opened), presets=presets, agents=agents)

    session = await runtime.open_session("session-test")

    assert session is opened
    assert presets.ensured == [("ptc", 7)]
    assert agents.calls[0]["preset_revision"] == 7
    assert agents.calls[0]["preset"] == "ptc"
    assert runtime.preset_revision == 7
    assert runtime.agent_preset == "ptc"
    assert runtime.agent_presentation == "ptc"
    assert runtime.agent_preset_display == "PTC"


@pytest.mark.asyncio
async def test_open_session_falls_back_when_the_revision_is_gone() -> None:
    opened = FakeSession("ptc")
    opened.records = [SimpleNamespace(type="agent-preset/selected", data={"preset": "ptc", "revision": 7})]
    presets = FakePresets()
    agents = FakeAgents()

    def create(**kwargs: Any) -> FakeAgent:
        if kwargs.get("preset_revision") is not None:
            raise PluginDependencyError("unknown revision")
        agents.calls.append(kwargs)
        return FakeAgent(11, presentation="ptc")

    agents.create = create  # type: ignore[method-assign]
    runtime = make_runtime(store=FakeStore(opened), presets=presets, agents=agents)

    await runtime.open_session("session-test")

    assert presets.ensured == [("ptc", 7), ("ptc", None)]
    assert agents.calls[0]["preset_revision"] is None
    assert runtime.preset_revision == 11
    assert runtime.agent_preset == "ptc"


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

    agents.create = lambda **kwargs: FakeAgent(11, presentation="ptc")  # type: ignore[method-assign]
    switched = await runtime.set_agent_preset("ptc")

    assert switched == "ptc"
    assert service.ensured == [("ptc", None)]
    assert runtime.agent_preset == "ptc"
    assert runtime.agent_presentation == "ptc"
    assert runtime.agent_preset_display == "PTC"
    assert session.records[-1].type == "agent-preset/selected"
    assert session.records[-1].data == {"preset": "ptc", "mode": "default", "revision": 11}
    assert previous.closed is True


@pytest.mark.asyncio
async def test_visible_presets_hide_team_from_the_picker() -> None:
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())

    assert [spec.id for spec in runtime.presets] == ["standard", "ptc", "minimal", "cordis"]


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
    assert runtime.team_previous_preset == "standard"
    assert previous.closed is True
    assert session.records[-1].data == {"preset": "team", "mode": "team", "previous": "standard"}

    restored = await runtime.set_team_mode(False)

    assert restored == "standard"
    assert runtime.agent_preset == "standard"
    assert runtime.agent_mode == "default"
    assert session.records[-1].data == {"preset": "standard", "mode": "default"}


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
            data={"preset": "team", "mode": "team", "previous": "ptc"},
        )
    ]
    presets = FakePresets()
    runtime = make_runtime(store=FakeStore(opened), presets=presets, agents=FakeAgents())

    await runtime.open_session("session-test")

    assert presets.ensured == [("team", None)]
    assert runtime.agent_preset == "team"
    assert runtime.agent_mode == "team"
    assert runtime.team_previous_preset == "ptc"


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
        await runtime.set_agent_preset("ptc")

    assert runtime.agent_preset == "standard"
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
    runtime.agent_preset = "ptc"

    await runtime.set_team_mode(True)

    assert runtime.team_previous_preset == "ptc"
    assert session.records[-1].data == {"preset": "team", "mode": "team", "previous": "ptc"}

    restored = await runtime.set_team_mode(False)

    assert restored == "ptc"
    assert runtime.agent_preset == "ptc"
    assert session.records[-1].data == {"preset": "ptc", "mode": "default"}


@pytest.mark.asyncio
async def test_open_session_normalizes_the_legacy_default_preset() -> None:
    opened = FakeSession("default")
    opened.records = [SimpleNamespace(type="agent-preset/selected", data={"preset": "default"})]
    presets = FakePresets()
    runtime = make_runtime(store=FakeStore(opened), presets=presets, agents=FakeAgents())

    await runtime.open_session("session-test")

    assert runtime.agent_preset == "standard"
    assert runtime.agent_presentation == "native"
    assert runtime.agent_preset_display == "Standard"
    assert presets.ensured == [("standard", None)]


@pytest.mark.asyncio
async def test_open_session_normalizes_a_legacy_header_without_records() -> None:
    opened = FakeSession("default")
    runtime = make_runtime(store=FakeStore(opened), presets=FakePresets(), agents=FakeAgents())

    await runtime.open_session("session-test")

    assert runtime.agent_preset == "standard"


@pytest.mark.asyncio
async def test_agent_presentation_falls_back_to_the_resolved_spec() -> None:
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())
    runtime.agent = None

    runtime.agent_preset = "ptc"
    assert runtime.agent_presentation == "ptc"

    runtime.agent_preset = "standard"
    assert runtime.agent_presentation == "native"


@pytest.mark.asyncio
async def test_scoped_registry_uses_the_canonical_preset_scope() -> None:
    scopes: list[Any] = []
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())

    def registry_for(cwd: Path, scope: str | None = None) -> object:
        scopes.append(scope)
        return object()

    runtime.kernel_app._services["tools"] = SimpleNamespace(registry_for=registry_for)
    runtime.agent_preset = "default"

    runtime._scoped_registry("D:/work")

    assert scopes == ["standard"]


@pytest.mark.asyncio
async def test_set_agent_preset_accepts_the_legacy_default_alias() -> None:
    session = FakeSession()
    presets = FakePresets()
    agents = FakeAgents()
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=presets, agents=agents)
    runtime.session = session
    runtime.agent = FakeAgent(0)
    runtime.agent_preset = "ptc"

    switched = await runtime.set_agent_preset("default")

    assert switched == "standard"
    assert runtime.agent_preset == "standard"
    assert session.records[-1].data == {"preset": "standard", "mode": "default"}


@pytest.mark.asyncio
async def test_set_agent_preset_same_canonical_id_is_a_no_op() -> None:
    runtime = make_runtime(store=FakeStore(FakeSession()), presets=FakePresets(), agents=FakeAgents())
    runtime.session = FakeSession()
    runtime.agent = FakeAgent(0)
    runtime.agent_preset = "standard"
    runtime.preset_revision = None

    switched = await runtime.set_agent_preset("default")

    assert switched == "standard"
    assert runtime.agent_preset == "standard"
    assert runtime.session.records == []
