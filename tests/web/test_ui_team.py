"""Session-level team coverage for the web host.

These tests pin the team wiring the SPA relies on: TeamService/SubagentService
are configured and bound to the native session exactly like the TUI does, the
persisted team facts are restored once per attach, detach stops both managers,
the read-only team snapshot is exposed over HTTP, the team preset is not offered
as a starting preset, and /team on|off toggles the mode inside a session.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from deeppyro.config import Config
from deeppyro.session.native_index import NativeSessionInfo
from deeppyro.session.native_query import NATIVE_QUERY_NOT_FOUND, NativeQueryError
from deeppyro.team.types import (
    MAX_MEMBERS,
    PHASE_COMPLETED,
    PHASE_IDLE,
    PHASE_RUNNING,
    MemberInfo,
    TaskInfo,
)
from deeppyro.web import service as service_module
from deeppyro.web.service import Selection, SessionBinding, WebSessionHost
from deeppyro.web.ui import server as ui_server
from deeppyro.web.ui.api import UiContext
from deeppyro.web.ui.auth import cookie_name
from deeppyro.web.ui.commands import STATUS_ERROR, STATUS_OK, execute
from deeppyro.web.wire import Conflict, CreateSessionRequest, NotFound, SendMessageRequest

TOKEN = "test-token-0123456789"
PORT = 8765
COOKIE = {"cookie": f"{cookie_name(PORT)}={TOKEN}"}

TEAM_OFF = "Team mode is off · use /team on or the Agent team row to enable it."


# -- fixtures --------------------------------------------------------------


def _config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "model-fast", "max_tokens": 1024},
            "providers": {
                "local": {
                    "api": "openai-completions",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key_env": "DEEPPYRO_LOCAL_API_KEY",
                    "models": [{"id": "model-fast", "context_window": 32768}],
                }
            },
        }
    )


def _info(
    identifier: str,
    cwd: str | Path = "D:/work",
    *,
    preset: str = "standard",
    turns: int = 1,
) -> NativeSessionInfo:
    return NativeSessionInfo(
        id=identifier,
        path=Path(f"{identifier}.jsonl"),
        cwd=str(cwd),
        created_at=1_000,
        updated_at=2_000,
        title=f"Session {identifier}",
        turns=turns,
        event_count=2,
        preset=preset,
        parent_id=None,
        origin="user",
        preview=None,
    )


def _preset(
    preset_id: str,
    mode: str,
    *,
    hidden: bool = False,
    revision: int = 0,
    order: int = 100,
) -> Any:
    return SimpleNamespace(
        id=preset_id,
        name=preset_id.title(),
        description=f"{preset_id} preset",
        model="",
        mode=mode,
        hidden=hidden,
        order=order,
        revision=revision,
    )


def _state(*, preset: str = "standard", mode: str = "default", cwd: str = "D:/work") -> Any:
    return SimpleNamespace(
        title="Session one",
        cwd=cwd,
        model=SimpleNamespace(provider="local", model="model-fast"),
        preset=SimpleNamespace(preset=preset, mode=mode, revision=0),
        permission_policy="ask",
        sandbox_mode="workspace-write",
        plan_mode=False,
        rounds_started=2,
        goal=None,
        todos=[],
        deliverables=[],
    )


class FakeSessions:
    def __init__(self, infos: list[NativeSessionInfo]) -> None:
        self._infos = list(infos)
        self.created: list[Any] = []
        self.deleted: list[str] = []
        self.user_message_ids: set[str] = set()

    def native_look(self, session_id: str) -> NativeSessionInfo:
        for info in self._infos:
            if info.id == session_id:
                return info
        raise NativeQueryError(NATIVE_QUERY_NOT_FOUND, f"native session not found: {session_id}")

    def native_read(self, session_id: str) -> Any:
        info = self.native_look(session_id)
        events = (SimpleNamespace(type="user.message"),) if session_id in self.user_message_ids else ()
        return SimpleNamespace(header=SimpleNamespace(cwd=info.cwd), events=events)

    def native_list(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[NativeSessionInfo]:
        return list(self._infos)

    def native_create(self, request: Any, *, deferred: bool = False) -> Any:
        self.created.append(request)
        return object()

    def native_open(self, session_id: str) -> Any:
        self.native_look(session_id)
        return object()

    def native_delete(self, session_id: str) -> NativeSessionInfo:
        info = self.native_look(session_id)
        self.deleted.append(session_id)
        self._infos = [row for row in self._infos if row.id != session_id]
        return info

    def invalidate_list_cache(self) -> None:
        pass


class FakeView:
    def __init__(self, state: Any | None = None) -> None:
        self.state = state if state is not None else _state()
        self.messages: list[Any] = []
        self.appended: list[tuple[str, dict[str, Any]]] = []
        self.header = SimpleNamespace(cwd="D:/work")

    def fold(self) -> Any:
        return SimpleNamespace(state=self.state, tasks=[], handoffs=[])

    def derive_messages(self) -> list[Any]:
        return []

    def append(self, record_type: str, data: dict[str, Any] | None = None) -> int:
        self.appended.append((record_type, dict(data or {})))
        return len(self.appended)


class FakeHost:
    def __init__(self) -> None:
        self.busy = False
        self.closed = False

    def usage(self) -> dict[str, int]:
        return {"input": 10, "output": 2, "cached": 3}

    def context_tokens(self) -> int:
        return 40

    async def interrupt(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class FakeAgents:
    def __init__(self) -> None:
        self.built: list[tuple[Any, Any]] = []

    def host(self, native_session: Any, build: Any) -> FakeHost:
        self.built.append((native_session, build))
        return FakeHost()


class FakeGate:
    approval = "ask"
    policy = None

    def __init__(self) -> None:
        self.bound: list[Any] = []

    def bind_session(self, view: Any) -> None:
        self.bound.append(view)


class FakeSandbox:
    def __init__(self) -> None:
        self.gates: list[FakeGate] = []

    def gate(self, workspace: Path, *, ask: Any) -> FakeGate:
        gate = FakeGate()
        self.gates.append(gate)
        return gate


class FakePermissionPresets:
    default = "standard"

    def get(self, name: str) -> Any:
        return SimpleNamespace(name=name, sandbox="workspace-write", approval="ask")

    def derive(self, view: Any) -> str:
        return "standard"

    def catalog(self) -> list[Any]:
        return [
            SimpleNamespace(
                name="standard",
                sandbox="workspace-write",
                approval="ask",
                description="standard permissions",
            )
        ]


class FakePresets:
    def __init__(self, specs: list[Any]) -> None:
        self._specs = {spec.id: spec for spec in specs}

    def all(self) -> list[Any]:
        return list(self._specs.values())

    async def ensure(self, preset: str) -> Any:
        return self._specs.get(preset) or self._specs["standard"]


class FakeTeamService:
    max_members = MAX_MEMBERS

    def __init__(
        self,
        *,
        members: list[MemberInfo] | None = None,
        tasks: list[TaskInfo] | None = None,
    ) -> None:
        self.configure_calls: list[dict[str, Any]] = []
        self.bound_sessions: list[Any] = []
        self.bound_parents: list[Any] = []
        self.restores = 0
        self.stops = 0
        self._members = list(members or [])
        self._tasks = list(tasks or [])

    def configure(self, **kwargs: Any) -> None:
        self.configure_calls.append(kwargs)

    def bind_session(self, session: Any) -> None:
        self.bound_sessions.append(session)

    def bind_parent(self, parent: Any) -> None:
        self.bound_parents.append(parent)

    async def restore(self) -> tuple[MemberInfo, ...]:
        self.restores += 1
        return tuple(self._members)

    async def stop(self) -> None:
        self.stops += 1

    def members(self) -> list[MemberInfo]:
        return list(self._members)

    def tasks(self) -> list[TaskInfo]:
        return list(self._tasks)


class FakeLlm:
    """Stand-in for the llm plugin provider factory."""

    def __init__(self, *, name: str = "local", broken: bool = False) -> None:
        self.calls: list[str] = []
        self._name = name
        self._broken = broken

    def native_provider(self, name: str) -> Any:
        self.calls.append(name)
        if self._broken:
            raise RuntimeError("provider is unavailable")
        return SimpleNamespace(name=self._name)


class FakeSubagentService:
    def __init__(self) -> None:
        self.configure_calls: list[dict[str, Any]] = []
        self.bound_sessions: list[Any] = []
        self.bound_parents: list[Any] = []
        self.closes = 0
        self.cancels = 0

    def configure(self, **kwargs: Any) -> None:
        self.configure_calls.append(kwargs)

    def bind_session(self, session: Any) -> None:
        self.bound_sessions.append(session)

    def bind_parent(self, parent: Any) -> None:
        self.bound_parents.append(parent)

    async def cancel_all(self) -> None:
        self.cancels += 1

    async def close(self) -> None:
        self.closes += 1


def _host(
    tmp_path: Path,
    *,
    infos: list[NativeSessionInfo] | None = None,
    config: Config | None = None,
    presets: FakePresets | None = None,
    tool_services: dict[str, Any] | None = None,
    agents: FakeAgents | None = None,
    llm: Any | None = None,
) -> tuple[WebSessionHost, FakeSessions]:
    sessions = FakeSessions(infos or [])
    services = SimpleNamespace(
        config=config if config is not None else _config(),
        sessions=sessions,
        agents=agents if agents is not None else FakeAgents(),
        presets=presets if presets is not None else FakePresets([_preset("standard", "default")]),
        llm=llm if llm is not None else SimpleNamespace(),
        sandbox=FakeSandbox(),
        tools=SimpleNamespace(),
        permission_presets=FakePermissionPresets(),
        working_directory=None,
        tool_services=tool_services if tool_services is not None else {},
        usage=None,
    )
    host = WebSessionHost(services, default_cwd=tmp_path)  # type: ignore[arg-type]
    return host, sessions


def _port() -> ui_server._PortRef:
    port_ref = ui_server._PortRef()
    port_ref.value = PORT
    return port_ref


def _client(service: Any) -> httpx.AsyncClient:
    app = ui_server.create_app(
        service_provider=lambda: service,
        context=UiContext(token=TOKEN, version="test"),
        port=_port(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1",
        headers=COOKIE,
    )


def _context() -> Any:
    return SimpleNamespace(kernel_app=None)


async def _attach(
    host: WebSessionHost,
    workspace: Path,
    *,
    session_id: str = "session-1",
    spec: Any | None = None,
    view: FakeView | None = None,
) -> tuple[SessionBinding, Any]:
    team_spec = spec if spec is not None else _preset("team", "team", hidden=True)
    native = object()
    handle = SimpleNamespace(session_id=session_id, native_session=native, aclose=lambda: None)
    bound_view = (
        view if view is not None else FakeView(_state(preset="team", mode="team", cwd=str(workspace)))
    )
    binding = await host._attach(
        handle,  # type: ignore[arg-type]
        bound_view,  # type: ignore[arg-type]
        workspace=workspace,
        selection=Selection("local", "model-fast", 32768),
        spec=team_spec,
        record_selection=False,
    )
    return binding, native


# -- team wiring -----------------------------------------------------------


async def test_attach_wires_team_and_subagent_and_restores_once(tmp_path: Path) -> None:
    team = FakeTeamService()
    subagent = FakeSubagentService()
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        tool_services={"team": team, "subagent": subagent},
    )

    binding, native = await _attach(host, tmp_path)

    assert binding.mode == "team"
    assert host.active_session_id == "session-1"
    assert len(team.configure_calls) == 1
    configure = team.configure_calls[0]
    assert configure["cwd"] == tmp_path
    assert configure["provider"] == "local"
    assert configure["model"] == "model-fast"
    assert configure["context_window"] == 32768
    assert configure["preset"] == "team"
    assert configure["preset_revision"] == 0
    assert configure["registry"] is None
    assert configure["permission"] is binding.gate
    assert configure["services"] == host.services.tool_services
    assert callable(configure["ask"])
    assert team.bound_sessions == [native]
    assert team.bound_parents == [binding.host]
    assert len(subagent.configure_calls) == 1
    assert subagent.configure_calls[0]["preset"] == "team"
    assert subagent.bound_sessions == [native]
    assert subagent.bound_parents == [binding.host]
    assert team.restores == 1

    # Re-attaching the same session must not fold the persisted team facts twice.
    await _attach(host, tmp_path)
    assert team.restores == 1

    await host._detach()
    assert host.active_session_id is None
    assert team.stops == 1
    # Detach must cancel subagent work, never close the process-wide manager:
    # a closed manager poisons every later attach with "service is closed".
    assert subagent.closes == 0
    assert subagent.cancels == 1
    assert subagent.bound_sessions[-1] is None
    assert subagent.bound_parents[-1] is None
    assert "session-1" not in host._restored_team_sessions

    # After detach the guard is cleared, so a later attach restores again.
    await _attach(host, tmp_path)
    assert team.restores == 2


async def test_attach_passes_resolved_provider_objects_to_tool_services(tmp_path: Path) -> None:
    """The subagent manager needs a provider object, not a bare name.

    It derives the child provider name from the object's name attribute, so a
    string makes every spawn fail with "native subagent service requires a
    provider" even though the session itself is configured.
    """
    team = FakeTeamService()
    subagent = FakeSubagentService()
    llm = FakeLlm()
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        tool_services={"team": team, "subagent": subagent},
        llm=llm,
    )

    await _attach(host, tmp_path)

    assert llm.calls == ["local"]
    team_provider = team.configure_calls[0]["provider"]
    subagent_provider = subagent.configure_calls[0]["provider"]
    assert team_provider is subagent_provider
    assert getattr(team_provider, "name", None) == "local"


async def test_attach_falls_back_to_the_provider_name_when_resolution_fails(tmp_path: Path) -> None:
    """A provider that cannot be built must not break attaching a session."""
    subagent = FakeSubagentService()
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        tool_services={"subagent": subagent},
        llm=FakeLlm(broken=True),
    )

    await _attach(host, tmp_path)

    assert host.active_session_id == "session-1"
    assert subagent.configure_calls[0]["provider"] == "local"


async def test_create_session_with_the_team_preset_stays_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team = FakeTeamService()
    presets = FakePresets([_preset("standard", "default"), _preset("team", "team", hidden=True)])
    host, sessions = _host(
        tmp_path,
        infos=[_info("session-new", tmp_path, preset="team")],
        presets=presets,
        tool_services={"team": team, "subagent": FakeSubagentService()},
    )
    state = _state(preset="team", mode="team", cwd=str(tmp_path))
    monkeypatch.setattr(
        service_module,
        "NativeSessionHandle",
        lambda native: SimpleNamespace(session_id="session-new", native_session=native, aclose=lambda: None),
    )
    monkeypatch.setattr(service_module, "_NativeView", lambda handle: FakeView(state))

    created = await host.create_session(
        CreateSessionRequest(cwd="", preset="team", provider="", model="", title="")
    )

    assert created["preset"] == "team"
    assert created["session_id"] == "session-new"
    assert host.active_session_id == "session-new"
    assert team.restores == 1
    assert sessions.created and sessions.created[0].preset == "team"

    timeline = await host.timeline("session-new")
    assert timeline["preset"] == "team"
    assert timeline["state"]["preset"] == {"preset": "team", "mode": "team", "revision": 0}


async def test_resume_session_with_the_team_preset_stays_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team = FakeTeamService()
    presets = FakePresets([_preset("standard", "default"), _preset("team", "team", hidden=True)])
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path, preset="team")],
        presets=presets,
        tool_services={"team": team, "subagent": FakeSubagentService()},
    )
    state = _state(preset="team", mode="team", cwd=str(tmp_path))
    monkeypatch.setattr(
        service_module,
        "NativeSessionHandle",
        lambda native: SimpleNamespace(session_id="session-1", native_session=native, aclose=lambda: None),
    )
    monkeypatch.setattr(service_module, "_NativeView", lambda handle: FakeView(state))
    monkeypatch.setattr(service_module, "fold", lambda header, events: FakeView(state))

    resumed = await host.resume_session("session-1")

    assert resumed["resumed"] is True
    assert resumed["preset"] == "team"
    assert host.active_session_id == "session-1"
    assert team.restores == 1


async def test_send_message_attaches_a_previewed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    team = FakeTeamService()
    subagent = FakeSubagentService()
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path), _info("session-2", tmp_path)],
        presets=FakePresets([_preset("standard", "default"), _preset("team", "team", hidden=True)]),
        tool_services={"team": team, "subagent": subagent},
    )
    await _attach(host, tmp_path, session_id="session-1")
    assert host.active_session_id == "session-1"

    # The browser only previews session-2; the first message must attach it.
    monkeypatch.setattr(
        service_module,
        "NativeSessionHandle",
        lambda native: SimpleNamespace(session_id="session-2", native_session=native, aclose=lambda: None),
    )
    monkeypatch.setattr(
        service_module,
        "_NativeView",
        lambda handle: FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )
    monkeypatch.setattr(host, "_start_turn_worker", lambda binding: None)

    result = await host.send_message("session-2", SendMessageRequest(text="hello"))

    assert result == {"session_id": "session-2", "accepted": True, "queued": 0}
    assert host.active_session_id == "session-2"
    binding = host._binding
    assert binding is not None
    assert [turn.text for turn in binding.pending] == ["hello"]
    # The switch really detached session-1 (its subagent work was cancelled).
    assert subagent.cancels == 1
    assert subagent.closes == 0


async def test_list_sessions_hides_the_attached_draft_until_first_message(tmp_path: Path) -> None:
    host, sessions = _host(tmp_path, infos=[_info("session-1", tmp_path, turns=0)])

    await _attach(host, tmp_path)

    page = await host.list_sessions()

    # The open draft is an input target, not a conversation: it appears only
    # once the user has actually sent something.
    assert page["sessions"] == []
    assert page["total"] == 0

    sessions.user_message_ids.add("session-1")

    page = await host.list_sessions()

    assert [row["session_id"] for row in page["sessions"]] == ["session-1"]


async def test_detach_deletes_a_never_used_draft(tmp_path: Path) -> None:
    host, sessions = _host(tmp_path, infos=[_info("session-1", tmp_path, turns=0)])

    await _attach(host, tmp_path)
    assert host.active_session_id == "session-1"

    await host._detach()

    assert host.active_session_id is None
    assert sessions.deleted == ["session-1"]


# -- read-only team snapshot ----------------------------------------------


def _members() -> list[MemberInfo]:
    return [
        MemberInfo(id="m1", name="alpha", phase=PHASE_IDLE),
        MemberInfo(id="m2", name="beta", phase=PHASE_RUNNING, provider="local", model="model-fast"),
    ]


def _tasks() -> list[TaskInfo]:
    return [
        TaskInfo(id="t1", subject="first", status="completed"),
        TaskInfo(id="t2", subject="second", status="in_progress"),
        TaskInfo(id="t3", subject="third", status="pending", blocked_by=("t2",)),
    ]


async def test_team_state_of_the_attached_team_session(tmp_path: Path) -> None:
    team = FakeTeamService(members=_members(), tasks=_tasks())
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path, preset="team")],
        tool_services={"team": team},
    )
    await _attach(host, tmp_path)

    snapshot = host.team_state("session-1")

    assert snapshot["session_id"] == "session-1"
    assert snapshot["active"] is True
    assert snapshot["mode"] == "team"
    assert snapshot["max_members"] == MAX_MEMBERS
    assert [member["id"] for member in snapshot["members"]] == ["m1", "m2"]
    assert set(snapshot["members"][0]) == {
        "id",
        "name",
        "description",
        "provider",
        "model",
        "preset",
        "phase",
        "session_id",
        "last_reply",
        "error",
        "created_at",
    }
    assert snapshot["members"][1]["phase"] == PHASE_RUNNING
    assert [task["id"] for task in snapshot["tasks"]] == ["t1", "t2", "t3"]
    assert snapshot["tasks"][2] == {
        "id": "t3",
        "revision": 1,
        "subject": "third",
        "description": "",
        "status": "pending",
        "blocked_by": ["t2"],
        "write_scopes": [],
        "owner": "",
        "created_at": 0,
    }


async def test_team_state_of_a_known_but_unattached_session(tmp_path: Path) -> None:
    team = FakeTeamService()
    host, _ = _host(
        tmp_path,
        infos=[_info("session-2", tmp_path)],
        tool_services={"team": team},
    )

    snapshot = host.team_state("session-2")

    assert snapshot == {
        "session_id": "session-2",
        "active": False,
        "mode": "",
        "members": [],
        "tasks": [],
        "max_members": MAX_MEMBERS,
    }


async def test_team_state_of_an_unknown_session_raises_not_found(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, tool_services={"team": FakeTeamService()})

    with pytest.raises(NotFound):
        host.team_state("missing")


async def test_team_state_without_a_team_service_is_inactive(tmp_path: Path) -> None:
    host, _ = _host(tmp_path)

    snapshot = host.team_state("whatever")

    assert snapshot["active"] is False
    assert snapshot["mode"] == ""
    assert snapshot["members"] == []
    assert snapshot["tasks"] == []
    assert snapshot["max_members"] == MAX_MEMBERS


# -- HTTP surface ----------------------------------------------------------


async def test_team_route_returns_the_snapshot(tmp_path: Path) -> None:
    team = FakeTeamService(members=_members(), tasks=_tasks())
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path, preset="team")],
        tool_services={"team": team},
    )
    await _attach(host, tmp_path)

    async with _client(host) as client:
        resp = await client.get("/api/ui/sessions/session-1/team")

    assert resp.status_code == 200
    assert resp.json() == host.team_state("session-1")


async def test_team_route_reports_unknown_sessions(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, tool_services={"team": FakeTeamService()})

    async with _client(host) as client:
        resp = await client.get("/api/ui/sessions/missing/team")

    assert resp.status_code == 404


async def test_bootstrap_hides_the_team_preset(tmp_path: Path) -> None:
    """Team mode is switched per session, so the hidden preset is not a choice."""
    presets = FakePresets(
        [
            _preset("standard", "default", order=10),
            _preset("team", "team", hidden=True, order=200),
        ]
    )
    host, _ = _host(tmp_path, presets=presets)

    async with _client(host) as client:
        resp = await client.get("/api/ui/bootstrap")

    assert resp.status_code == 200
    body = resp.json()
    presets_by_id = {item["id"]: item for item in body["presets"]}
    assert set(presets_by_id) == {"standard"}
    assert presets_by_id["standard"]["hidden"] is False


# -- /team command ---------------------------------------------------------


async def test_team_command_reports_the_live_team(tmp_path: Path) -> None:
    team = FakeTeamService(members=_members(), tasks=_tasks())
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path, preset="team")],
        tool_services={"team": team},
    )
    await _attach(host, tmp_path)

    result = await execute(host, _context(), "team")

    assert result.status == STATUS_OK
    assert result.message == "Team mode is active · 2 members (idle, running) · 1/3 tasks done"
    assert len(result.data["members"]) == 2
    assert len(result.data["tasks"]) == 3
    assert result.data["max_members"] == MAX_MEMBERS


async def test_team_command_reports_team_mode_off(tmp_path: Path) -> None:
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        tool_services={"team": FakeTeamService()},
    )
    await _attach(
        host,
        tmp_path,
        spec=_preset("standard", "default"),
        view=FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )

    result = await execute(host, _context(), "team")

    assert result.status == STATUS_OK
    assert result.message == TEAM_OFF


async def test_team_command_turns_team_mode_on_and_off(tmp_path: Path) -> None:
    team = FakeTeamService()
    presets = FakePresets([_preset("standard", "default"), _preset("team", "team", hidden=True)])
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        presets=presets,
        tool_services={"team": team, "subagent": FakeSubagentService()},
    )
    await _attach(
        host,
        tmp_path,
        spec=_preset("standard", "default"),
        view=FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )

    enabled = await execute(host, _context(), "team", ["on"])

    assert enabled.status == STATUS_OK
    assert enabled.message == "Agent team enabled."
    assert enabled.data["enabled"] is True
    assert host._binding is not None and host._binding.mode == "team"

    disabled = await execute(host, _context(), "team", ["off"])

    assert disabled.status == STATUS_OK
    assert disabled.message == "Agent team turned off."
    assert disabled.data["enabled"] is False
    assert host._binding is not None and host._binding.preset == "standard"


async def test_team_command_rejects_unknown_actions(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    await _attach(host, tmp_path)

    result = await execute(host, _context(), "team", ["maybe"])

    assert result.status == STATUS_ERROR
    assert result.message == "usage: /team [on|off|status]"


async def test_team_command_without_a_session_is_an_error(tmp_path: Path) -> None:
    host, _ = _host(tmp_path, tool_services={"team": FakeTeamService()})

    result = await execute(host, _context(), "team")

    assert result.status == STATUS_ERROR
    assert result.message


# -- session-level team toggle ---------------------------------------------


def _two_presets() -> FakePresets:
    return FakePresets([_preset("standard", "default"), _preset("team", "team", hidden=True)])


async def test_team_toggle_enables_team_mode_in_a_session(tmp_path: Path) -> None:
    team = FakeTeamService()
    agents = FakeAgents()
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        presets=_two_presets(),
        agents=agents,
        tool_services={"team": team, "subagent": FakeSubagentService()},
    )
    await _attach(
        host,
        tmp_path,
        spec=_preset("standard", "default"),
        view=FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )
    before = len(agents.built)

    snapshot = await host.set_session_team("session-1", True)

    binding = host._binding
    assert binding is not None
    assert snapshot["ok"] is True
    assert snapshot["enabled"] is True
    assert snapshot["mode"] == "team"
    assert snapshot["preset"] == "team"
    assert snapshot["max_members"] == MAX_MEMBERS
    assert binding.preset == "team"
    assert binding.mode == "team"
    assert binding.team_previous_preset == "standard"
    assert len(agents.built) == before + 1
    assert agents.built[-1][1].preset == "team"
    assert team.restores == 1
    assert (
        "agent-preset/selected",
        {"preset": "team", "mode": "team", "revision": 0},
    ) in binding.view.appended


async def test_team_toggle_is_idempotent(tmp_path: Path) -> None:
    agents = FakeAgents()
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        presets=_two_presets(),
        agents=agents,
        tool_services={"team": FakeTeamService()},
    )
    await _attach(
        host,
        tmp_path,
        spec=_preset("standard", "default"),
        view=FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )
    await host.set_session_team("session-1", True)
    built = len(agents.built)

    snapshot = await host.set_session_team("session-1", True)

    assert snapshot["enabled"] is True
    assert len(agents.built) == built


async def test_team_toggle_disables_and_restores_the_previous_preset(tmp_path: Path) -> None:
    team = FakeTeamService(members=[MemberInfo(id="m1", name="alpha", phase=PHASE_COMPLETED)])
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path, preset="team")],
        presets=_two_presets(),
        tool_services={"team": team, "subagent": FakeSubagentService()},
    )
    await _attach(host, tmp_path)

    snapshot = await host.set_session_team("session-1", False)

    binding = host._binding
    assert binding is not None
    assert snapshot["enabled"] is False
    assert snapshot["mode"] == "default"
    assert binding.preset == "standard"
    assert binding.team_previous_preset == "standard"
    assert team.stops == 1
    assert (
        "agent-preset/selected",
        {"preset": "standard", "mode": "default", "revision": 0},
    ) in binding.view.appended


async def test_team_toggle_refuses_to_leave_while_members_are_live(tmp_path: Path) -> None:
    team = FakeTeamService(members=[MemberInfo(id="m1", name="beta", phase=PHASE_RUNNING)])
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path, preset="team")],
        presets=_two_presets(),
        tool_services={"team": team},
    )
    await _attach(host, tmp_path)

    with pytest.raises(Conflict, match="members are active"):
        await host.set_session_team("session-1", False)

    assert team.stops == 0


async def test_team_toggle_refuses_while_a_turn_is_running_or_queued(tmp_path: Path) -> None:
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path, preset="team")],
        presets=_two_presets(),
        tool_services={"team": FakeTeamService()},
    )
    binding, _ = await _attach(host, tmp_path)
    binding.host.busy = True

    with pytest.raises(Conflict, match="running or queued"):
        await host.set_session_team("session-1", False)


async def test_team_toggle_attaches_a_previewed_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, _ = _host(
        tmp_path,
        infos=[_info("session-2", tmp_path)],
        presets=_two_presets(),
        tool_services={"team": FakeTeamService(), "subagent": FakeSubagentService()},
    )
    monkeypatch.setattr(
        service_module,
        "NativeSessionHandle",
        lambda native: SimpleNamespace(session_id="session-2", native_session=native, aclose=lambda: None),
    )
    monkeypatch.setattr(
        service_module,
        "_NativeView",
        lambda handle: FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )
    monkeypatch.setattr(
        service_module,
        "fold",
        lambda header, events: FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )

    snapshot = await host.set_session_team("session-2", True)

    assert host.active_session_id == "session-2"
    assert snapshot["enabled"] is True


async def test_team_toggle_route_enables_and_validates(tmp_path: Path) -> None:
    host, _ = _host(
        tmp_path,
        infos=[_info("session-1", tmp_path)],
        presets=_two_presets(),
        tool_services={"team": FakeTeamService(), "subagent": FakeSubagentService()},
    )
    await _attach(
        host,
        tmp_path,
        spec=_preset("standard", "default"),
        view=FakeView(_state(preset="standard", mode="default", cwd=str(tmp_path))),
    )

    async with _client(host) as client:
        enabled = await client.post("/api/ui/sessions/session-1/team", json={"enabled": True})
        invalid = await client.post("/api/ui/sessions/session-1/team", json={"enabled": "yes"})
        missing = await client.post("/api/ui/sessions/missing/team", json={"enabled": True})

    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert invalid.status_code == 400
    assert missing.status_code == 404
