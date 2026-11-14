"""Cold team restore wiring: TuiRuntime replays team state once per session."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.tui.runtime import TuiRuntime


class FakeSession:
    def __init__(self, session_id: str, preset: str = "team") -> None:
        self.session_id = session_id
        self.header = SimpleNamespace(id=session_id, agentPreset=preset)
        self.records: list[Any] = []

    def append(self, record_type: str, data: dict[str, Any]) -> None:
        self.records.append(SimpleNamespace(type=record_type, data=data))


class FakeStore:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def open(self, session_id: str) -> FakeSession:
        return self.session

    def create(self, cwd: str, *, agent_preset: str | None = None) -> FakeSession:
        return self.session


class FakeAgent:
    preset_revision = None

    async def aclose(self) -> None:
        return None


class FakeAgents:
    def options(self, **overrides: Any) -> Any:
        return SimpleNamespace(**overrides)

    def create(self, **kwargs: Any) -> FakeAgent:
        return FakeAgent()


class FakePresets:
    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> Any:
        return SimpleNamespace(
            id=preset_id or "standard",
            revision=revision or 0,
            mode="team" if preset_id == "team" else "default",
        )

    def canonical(self, preset_id: str) -> str:
        return preset_id

    async def ensure(self, preset_id: str, *, revision: int | None = None) -> Any:
        return self.resolve(preset_id, revision=revision)


class FakeTeam:
    def __init__(self, *, fail: bool = False) -> None:
        self.restore_calls = 0
        self.fail = fail

    def configure(self, **kwargs: Any) -> None:
        return None

    def bind_session(self, session: Any) -> None:
        return None

    async def restore(self) -> tuple[Any, ...]:
        self.restore_calls += 1
        if self.fail:
            raise RuntimeError("broken team log")
        return ()


class FakeKernelApp:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services

    def service(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError:
            raise LookupError(name) from None


def make_runtime(team: FakeTeam, session: FakeSession) -> TuiRuntime:
    services: dict[str, Any] = {"presets": FakePresets(), "team": team}
    return TuiRuntime(
        cfg=SimpleNamespace(providers={}),
        cwd=Path("D:/work"),
        kernel_app=FakeKernelApp(services),
        provider_name="fake",
        model="fake-1",
        context_window=8192,
        store=FakeStore(session),
        provider=object(),
        registry=object(),
        permission=SimpleNamespace(),
        agents=FakeAgents(),
        sink=object(),
        ask=lambda request: None,
        team=team,
    )


async def test_open_team_session_restores_team_state() -> None:
    team = FakeTeam()
    runtime = make_runtime(team, FakeSession("session-1"))

    await runtime.open_session("session-1")

    assert team.restore_calls == 1


async def test_team_restore_runs_once_per_session() -> None:
    team = FakeTeam()
    session = FakeSession("session-1")
    runtime = make_runtime(team, session)

    await runtime.open_session("session-1")
    await runtime._attach(session)
    await runtime.open_session("session-1")

    assert team.restore_calls == 1


async def test_default_mode_session_does_not_restore_team() -> None:
    team = FakeTeam()
    runtime = make_runtime(team, FakeSession("session-1", preset="standard"))

    await runtime.open_session("session-1")

    assert team.restore_calls == 0


async def test_restore_failure_does_not_break_open_session() -> None:
    team = FakeTeam(fail=True)
    runtime = make_runtime(team, FakeSession("session-1"))

    session = await runtime.open_session("session-1")

    assert session.session_id == "session-1"
    assert team.restore_calls == 1
    assert runtime.agent is not None
