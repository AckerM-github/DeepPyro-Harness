"""TuiRuntime session lifecycle hooks: session/created and session/disposed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.hooks import SESSION_CREATED, SESSION_DISPOSED, HookService
from deeppyro.tui.runtime import TuiRuntime


class FakeSession:
    def __init__(self, session_id: str, preset: str = "default") -> None:
        self.session_id = session_id
        self.header = SimpleNamespace(agentPreset=preset)
        self.records: list[Any] = []

    def append(self, record_type: str, data: dict[str, Any]) -> None:
        self.records.append(SimpleNamespace(type=record_type, data=data))


class FakeStore:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []
        self._next = 1

    def create(self, cwd: str, *, agent_preset: str | None = None) -> FakeSession:
        session = FakeSession(f"session-{self._next}", agent_preset or "default")
        self._next += 1
        self.sessions.append(session)
        return session

    def open(self, session_id: str) -> FakeSession:
        raise AssertionError("open is not used here")


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
        return SimpleNamespace(id=preset_id or "default", revision=revision or 0, mode="default")

    def all(self) -> list[Any]:
        return []

    async def ensure(self, preset_id: str, *, revision: int | None = None) -> Any:
        return self.resolve(preset_id, revision=revision)

    async def ensure_all(self) -> None:
        return None


class FakeKernelApp:
    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services
        self.stopped = False

    def service(self, name: str) -> Any:
        try:
            return self._services[name]
        except KeyError:
            raise LookupError(name) from None

    async def stop(self) -> None:
        self.stopped = True


def make_runtime(*, hooks: HookService | None = None) -> tuple[TuiRuntime, FakeStore]:
    services: dict[str, Any] = {"presets": FakePresets()}
    if hooks is not None:
        services["hooks"] = hooks
    store = FakeStore()
    runtime = TuiRuntime(
        cfg=SimpleNamespace(providers={}),
        cwd=Path("D:/work"),
        kernel_app=FakeKernelApp(services),
        provider_name="fake",
        model="fake-1",
        context_window=8192,
        store=store,
        provider=object(),
        registry=object(),
        permission=SimpleNamespace(),
        agents=FakeAgents(),
        sink=object(),
        ask=lambda request: None,
    )
    return runtime, store


async def test_new_session_emits_session_created() -> None:
    hooks = HookService()
    events: list[dict[str, Any]] = []
    hooks.on(SESSION_CREATED, lambda payload: events.append(dict(payload)))
    runtime, store = make_runtime(hooks=hooks)

    session = await runtime.new_session()

    assert len(store.sessions) == 1
    assert events == [{"sessionId": "session-1", "cwd": str(runtime.cwd), "preset": "default"}]
    assert runtime._created_sessions == {"session-1"}
    assert session.session_id == "session-1"


async def test_create_schedule_session_emits_session_created(tmp_path: Path) -> None:
    hooks = HookService()
    events: list[dict[str, Any]] = []
    hooks.on(SESSION_CREATED, lambda payload: events.append(dict(payload)))
    runtime, _ = make_runtime(hooks=hooks)

    session = await runtime.create_schedule_session(SimpleNamespace(cwd=str(tmp_path)), "reviewer")

    expected_cwd = str(tmp_path.expanduser().resolve())
    assert events == [{"sessionId": session.session_id, "cwd": expected_cwd, "preset": "reviewer"}]


async def test_close_emits_one_disposed_per_created_session() -> None:
    hooks = HookService()
    created: list[dict[str, Any]] = []
    disposed: list[dict[str, Any]] = []
    hooks.on(SESSION_CREATED, lambda payload: created.append(dict(payload)))
    hooks.on(SESSION_DISPOSED, lambda payload: disposed.append(dict(payload)))
    runtime, _ = make_runtime(hooks=hooks)
    first = await runtime.new_session()
    second = await runtime.new_session()

    await runtime.close()

    assert [event["sessionId"] for event in created] == ["session-1", "session-2"]
    assert [event["sessionId"] for event in disposed] == ["session-1", "session-2"]
    assert runtime.kernel_app is None
    assert runtime._closed is True

    await runtime.close()
    assert len(disposed) == 2
    assert first.session_id == "session-1" and second.session_id == "session-2"


async def test_close_without_hook_listeners_is_silent() -> None:
    hooks = HookService()
    runtime, _ = make_runtime(hooks=hooks)
    await runtime.new_session()
    await runtime.close()
    assert runtime.kernel_app is None


async def test_close_without_hooks_service_is_silent() -> None:
    runtime, _ = make_runtime()
    await runtime.new_session()
    await runtime.close()
    assert runtime.kernel_app is None


async def test_session_created_listener_failure_does_not_break_new_session() -> None:
    hooks = HookService()

    def explode(payload: dict[str, Any]) -> None:
        raise RuntimeError("listener broke")

    hooks.on(SESSION_CREATED, explode)
    runtime, _ = make_runtime(hooks=hooks)

    session = await runtime.new_session()

    assert session.session_id == "session-1"
    assert runtime._created_sessions == {"session-1"}


async def test_session_disposed_listener_failure_does_not_break_close() -> None:
    hooks = HookService()

    def explode(payload: dict[str, Any]) -> None:
        raise RuntimeError("listener broke")

    hooks.on(SESSION_DISPOSED, explode)
    runtime, _ = make_runtime(hooks=hooks)
    await runtime.new_session()

    await runtime.close()

    assert runtime.kernel_app is None
    assert runtime._closed is True


async def test_created_sessions_include_scheduled_ones() -> None:
    hooks = HookService()
    disposed: list[dict[str, Any]] = []
    hooks.on(SESSION_DISPOSED, lambda payload: disposed.append(dict(payload)))
    runtime, _ = make_runtime(hooks=hooks)
    await runtime.new_session()
    await runtime.create_schedule_session(SimpleNamespace(cwd=""), "default")

    await runtime.close()

    assert [event["sessionId"] for event in disposed] == ["session-1", "session-2"]


@pytest.mark.parametrize("bad_id", ["", None])
async def test_sessions_without_id_are_skipped(bad_id: Any) -> None:
    runtime, store = make_runtime()
    session = FakeSession("placeholder")
    session.session_id = bad_id  # type: ignore[assignment]
    store.create = lambda cwd, agent_preset=None: session  # type: ignore[method-assign]

    await runtime._session_created(session, cwd="D:/work", preset=None)

    assert runtime._created_sessions == set()
