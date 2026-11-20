"""HTTP and host-level coverage for the session extras.

These tests pin five behaviours the SPA relies on that the older API tests do
not cover: the folded "state" block on the timeline, retargeting one attached
session to another provider/model, the approval-scope round trip, the pending
turn queue with stable ids, and the read-only directory picker.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from deeppyro.config import Config
from deeppyro.sandbox.types import ApprovalRequest
from deeppyro.session.native_index import NativeSessionInfo
from deeppyro.session.native_query import NATIVE_QUERY_NOT_FOUND, NativeQueryError
from deeppyro.web import service as service_module
from deeppyro.web.service import (
    DEFAULT_CONTEXT_WINDOW,
    FS_ROOTS_LIMIT,
    PendingTurn,
    SessionBinding,
    WebSessionHost,
)
from deeppyro.web.ui import server as ui_server
from deeppyro.web.ui.api import UiContext
from deeppyro.web.ui.auth import cookie_name
from deeppyro.web.wire import BadRequest, NotFound, SendMessageRequest

TOKEN = "test-token-0123456789"
PORT = 8765
COOKIE = {"cookie": f"{cookie_name(PORT)}={TOKEN}"}

#: The serialised session facts the timeline "state" key must always carry.
_STATE_KEYS = {
    "title",
    "cwd",
    "model",
    "preset",
    "permission_policy",
    "sandbox_mode",
    "plan_mode",
    "rounds_started",
    "goal",
    "todos",
    "deliverables",
    "tasks",
    "handoffs",
}


# -- fixtures --------------------------------------------------------------


def _config() -> Config:
    """A two-provider config so model switches are exercised across providers."""
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "model-fast", "max_tokens": 1024},
            "providers": {
                "local": {
                    "api": "openai-completions",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key_env": "DEEPPYRO_LOCAL_API_KEY",
                    "models": [
                        {"id": "model-fast", "context_window": 32768},
                        {"id": "model-slow", "context_window": 65536},
                    ],
                },
                "remote": {
                    "api": "openai-completions",
                    "base_url": "http://127.0.0.1:9000/v1",
                    "api_key_env": "DEEPPYRO_REMOTE_API_KEY",
                    "models": [{"id": "remote-large", "context_window": 131072}],
                },
            },
        }
    )


def _info(identifier: str, cwd: str | Path = "D:/work", *, preset: str = "standard") -> NativeSessionInfo:
    return NativeSessionInfo(
        id=identifier,
        path=Path(f"{identifier}.jsonl"),
        cwd=str(cwd),
        created_at=1_000,
        updated_at=2_000,
        title=f"Session {identifier}",
        turns=1,
        event_count=2,
        preset=preset,
        parent_id=None,
        origin="user",
        preview=None,
    )


def _state() -> Any:
    """A folded-state duck type carrying every field _state_facts reads."""
    return SimpleNamespace(
        title="Session one",
        cwd="D:/work",
        model=SimpleNamespace(provider="local", model="model-fast"),
        preset=SimpleNamespace(preset="standard", mode="single", revision=3),
        permission_policy="ask",
        sandbox_mode="workspace-write",
        plan_mode=False,
        rounds_started=2,
        goal=None,
        todos=[SimpleNamespace(content="ship it", status="in_progress")],
        deliverables=[SimpleNamespace(path="out.txt", description="the deliverable")],
    )


class FakeSessions:
    """Read-only sessions plugin surface the host reads."""

    def __init__(self, infos: list[NativeSessionInfo]) -> None:
        self._infos = list(infos)
        self.read_calls: list[str] = []
        self.invalidations = 0

    def native_look(self, session_id: str) -> NativeSessionInfo:
        for info in self._infos:
            if info.id == session_id:
                return info
        raise NativeQueryError(NATIVE_QUERY_NOT_FOUND, f"native session not found: {session_id}")

    def native_read(self, session_id: str) -> Any:
        self.read_calls.append(session_id)
        info = self.native_look(session_id)
        return SimpleNamespace(header=SimpleNamespace(cwd=info.cwd), events=())

    def native_list(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[NativeSessionInfo]:
        return list(self._infos)

    def invalidate_list_cache(self) -> None:
        self.invalidations += 1


class FakeView:
    """Folded session view the host reads while a session is attached."""

    def __init__(
        self,
        state: Any | None = None,
        *,
        tasks: list[Any] | None = None,
        handoffs: list[Any] | None = None,
    ) -> None:
        self.state = state if state is not None else _state()
        self.messages: list[Any] = []
        self.tasks: list[Any] = list(tasks or [])
        self.handoffs: list[Any] = list(handoffs or [])
        self.appended: list[tuple[str, dict[str, Any]]] = []
        self.header = SimpleNamespace(cwd="D:/work")

    def fold(self) -> Any:
        return SimpleNamespace(state=self.state, tasks=self.tasks, handoffs=self.handoffs)

    def derive_messages(self) -> list[Any]:
        return []

    def append(self, record_type: str, data: dict[str, Any] | None = None) -> int:
        self.appended.append((record_type, dict(data or {})))
        return len(self.appended)


class NativeViewProxy:
    """Mimic service._NativeView: explicit methods, other names forward to handle.

    The real proxy only types the methods the web host calls; reading any other
    attribute (tasks, handoffs, ...) falls through to NativeSessionHandle, which
    does not carry folded facts at all.
    """

    def __init__(self, view: FakeView) -> None:
        self._view = view
        self._handle = SimpleNamespace(session_id="session-1")

    def fold(self) -> Any:
        return self._view.fold()

    def derive_messages(self) -> list[Any]:
        return self._view.derive_messages()

    def append(self, record_type: str, data: dict[str, Any] | None = None) -> int:
        return self._view.append(record_type, data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)


class FakeHost:
    """The native agent host methods the web host calls."""

    def __init__(self) -> None:
        self.busy = False
        self.reconfigured: list[dict[str, Any]] = []
        self.sent: list[str] = []

    def reconfigure(self, **kwargs: Any) -> bool:
        self.reconfigured.append(kwargs)
        return True

    def usage(self) -> dict[str, int]:
        return {"input": 10, "output": 2, "cached": 3}

    def context_tokens(self) -> int:
        return 40

    async def send(self, text: str) -> Any:
        self.sent.append(text)
        return SimpleNamespace(final_text=f"echo: {text}", finish_reason="stop")

    async def interrupt(self) -> bool:
        return True

    def last_assistant_text(self) -> str:
        return ""

    def restore(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


class FakeLlm:
    def __init__(self) -> None:
        self.requested: list[str] = []

    def native_provider(self, name: str) -> Any:
        self.requested.append(name)
        return SimpleNamespace(name=name)


class FakePermissionPresets:
    def derive(self, view: Any) -> str:
        return "standard"


def _host(
    tmp_path: Path,
    *,
    infos: list[NativeSessionInfo] | None = None,
    config: Config | None = None,
) -> tuple[WebSessionHost, FakeSessions, FakeLlm]:
    sessions = FakeSessions(infos or [])
    llm = FakeLlm()
    services = SimpleNamespace(
        config=config if config is not None else _config(),
        sessions=sessions,
        agents=SimpleNamespace(),
        presets=SimpleNamespace(),
        llm=llm,
        sandbox=SimpleNamespace(),
        tools=SimpleNamespace(),
        permission_presets=FakePermissionPresets(),
        working_directory=None,
        tool_services={},
        usage=None,
    )
    host = WebSessionHost(services, default_cwd=tmp_path)  # type: ignore[arg-type]
    return host, sessions, llm


def _attach(
    host: WebSessionHost,
    workspace: Path,
    *,
    session_id: str = "session-1",
    busy: bool = False,
    state: Any | None = None,
) -> SessionBinding:
    view = FakeView(state)
    live = FakeHost()
    live.busy = busy
    binding = SessionBinding(
        handle=SimpleNamespace(session_id=session_id),  # type: ignore[arg-type]
        view=view,  # type: ignore[arg-type]
        host=live,  # type: ignore[arg-type]
        hooks=SimpleNamespace(reset=lambda: None),  # type: ignore[arg-type]
        gate=SimpleNamespace(approval="ask", policy=None),  # type: ignore[arg-type]
        provider="local",
        model="model-fast",
        preset="standard",
        preset_revision=3,
        context_window=32768,
        workspace=workspace,
    )
    host._binding = binding
    return binding


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


async def _wait_for_prompts(host: WebSessionHost, count: int = 1) -> list[dict[str, Any]]:
    for _ in range(1000):
        prompts = host.pending_approvals()
        if len(prompts) >= count:
            return prompts
        await asyncio.sleep(0)
    raise AssertionError(f"host never published {count} approval prompt(s)")


# -- A. timeline state -----------------------------------------------------


async def test_timeline_active_session_carries_the_folded_state(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    _attach(host, tmp_path)

    body = await host.timeline("session-1")

    assert set(body["state"]) == _STATE_KEYS
    assert body["state"] == {
        "title": "Session one",
        "cwd": "D:/work",
        "model": {"provider": "local", "model": "model-fast"},
        "preset": {"preset": "standard", "mode": "single", "revision": 3},
        "permission_policy": "ask",
        "sandbox_mode": "workspace-write",
        "plan_mode": False,
        "rounds_started": 2,
        "goal": None,
        "todos": [{"content": "ship it", "status": "in_progress"}],
        "deliverables": [{"path": "out.txt", "description": "the deliverable"}],
        "tasks": [],
        "handoffs": [],
    }
    # The pre-existing timeline contract stays intact next to the new key.
    assert body["active"] is True
    assert body["session_id"] == "session-1"
    assert body["title"] == "Session one"
    assert body["cwd"] == "D:/work"
    assert body["provider"] == "local"
    assert body["model"] == "model-fast"
    assert body["preset"] == "standard"
    assert body["permission_preset"] == "standard"
    assert body["context_tokens"] == 40
    assert body["context_window"] == 32768
    assert body["usage"] == {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 3}
    assert body["busy"] is False
    assert body["pending"] == 0
    assert body["messages"] == []


async def test_timeline_inactive_session_has_the_same_state_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, sessions, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    view = FakeView(_state())
    monkeypatch.setattr(service_module, "fold", lambda header, events: view)

    body = await host.timeline("session-1")

    assert set(body["state"]) == _STATE_KEYS
    assert body["state"]["title"] == "Session one"
    assert body["state"]["todos"] == [{"content": "ship it", "status": "in_progress"}]
    assert body["active"] is False
    assert body["context_window"] == DEFAULT_CONTEXT_WINDOW
    assert body["pending"] == 0
    assert sessions.read_calls == ["session-1"]


async def test_timeline_route_returns_state_and_journal_seq(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    _attach(host, tmp_path)

    async with _client(host) as client:
        response = await client.get("/api/ui/sessions/session-1/timeline")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body["state"]) == _STATE_KEYS
    assert body["state"]["model"] == {"provider": "local", "model": "model-fast"}
    assert body["state"]["todos"] == [{"content": "ship it", "status": "in_progress"}]
    assert body["active"] is True
    assert body["messages"] == []
    assert "journal_seq" in body


async def test_timeline_route_unknown_session_is_404(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path)

    async with _client(host) as client:
        response = await client.get("/api/ui/sessions/session-missing/timeline")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_timeline_active_path_tolerates_the_native_view_wrapper(tmp_path: Path) -> None:
    """Regression: timeline passed the _NativeView proxy where tasks are read.

    The proxy forwards unknown attribute reads to the native handle, so
    _state_facts used to raise AttributeError on every active-session timeline.
    """
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    binding = _attach(host, tmp_path)
    task = SimpleNamespace(
        task_id="task-1", objective="ship it", status="running", owner="agent", created_at=1.0
    )
    handoff = SimpleNamespace(
        run_id="run-1",
        task_id="task-1",
        from_owner="lead",
        to_owner="backend",
        summary="delegated",
        open_items=["finish"],
        result_ok=None,
        result=None,
        error=None,
        created_at=2.0,
    )
    folded = FakeView(state=_state(), tasks=[task], handoffs=[handoff])
    binding.view = NativeViewProxy(folded)  # type: ignore[assignment]

    body = await host.timeline("session-1")

    assert set(body["state"]) == _STATE_KEYS
    assert body["state"]["tasks"] == [
        {
            "task_id": "task-1",
            "objective": "ship it",
            "status": "running",
            "owner": "agent",
            "created_at": 1.0,
        }
    ]
    assert body["state"]["handoffs"] == [
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "from_owner": "lead",
            "to_owner": "backend",
            "summary": "delegated",
            "open_items": ["finish"],
            "result_ok": None,
            "result": None,
            "error": None,
            "created_at": 2.0,
        }
    ]


# -- B. session-scoped model switch ---------------------------------------


async def test_set_session_model_retargets_an_idle_session(tmp_path: Path) -> None:
    host, _, llm = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    binding = _attach(host, tmp_path)

    result = await host.set_session_model("session-1", "remote", "remote-large")

    assert result == {
        "ok": True,
        "session_id": "session-1",
        "provider": "remote",
        "model": "remote-large",
        "applied": "next_turn",
    }
    assert llm.requested == ["remote"]
    assert (binding.provider, binding.model, binding.context_window) == (
        "remote",
        "remote-large",
        131072,
    )
    assert binding.deferred_selection is None
    assert binding.view.appended == [("model/selection", {"provider": "remote", "model": "remote-large"})]
    assert len(binding.host.reconfigured) == 1
    changed = binding.host.reconfigured[0]
    assert changed["model"] == "remote-large"
    assert changed["provider"].name == "remote"
    assert changed["prompt_facts"].model == "remote-large"
    # The limits must travel with the reconfigure: NativeAgentHost and the loop
    # use distinct _UNSET sentinels, so omitted limits would be stored as values.
    config = host._services.config
    assert changed["temperature"] == config.agent.temperature
    assert changed["max_tokens_per_call"] == config.agent.max_tokens
    assert changed["max_steps"] == config.agent.max_steps


async def test_set_session_model_defers_while_streaming(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    binding = _attach(host, tmp_path, busy=True)

    result = await host.set_session_model("session-1", "remote", "remote-large")

    assert result["applied"] == "deferred"
    assert binding.deferred_selection is not None
    assert binding.host.reconfigured == []
    assert (binding.provider, binding.model) == ("local", "model-fast")

    # The next queued turn applies the choice before its own model call.
    binding.host.busy = False
    binding.pending.append(PendingTurn(queue_id="queued-1", text="next question", created_at=1.0))
    await host._run_turns(binding)

    assert binding.host.sent == ["next question"]
    assert (binding.provider, binding.model) == ("remote", "remote-large")
    assert binding.deferred_selection is None
    assert list(binding.pending) == []
    assert len(binding.host.reconfigured) == 1


async def test_model_route_switches_and_reports_deferred(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    binding = _attach(host, tmp_path)

    async with _client(host) as client:
        applied = await client.post(
            "/api/ui/sessions/session-1/model",
            json={"provider": "remote", "model": "remote-large"},
        )
        binding.host.busy = True
        deferred = await client.post(
            "/api/ui/sessions/session-1/model",
            json={"provider": "local", "model": "model-slow"},
        )

    assert applied.status_code == 200
    assert applied.headers["cache-control"] == "no-store"
    assert applied.json() == {
        "ok": True,
        "session_id": "session-1",
        "provider": "remote",
        "model": "remote-large",
        "applied": "next_turn",
    }
    assert deferred.status_code == 200
    assert deferred.json()["applied"] == "deferred"
    assert deferred.json()["provider"] == "local"
    assert deferred.json()["model"] == "model-slow"


async def test_model_route_error_envelope(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])

    async with _client(host) as client:
        unknown_session = await client.post(
            "/api/ui/sessions/session-missing/model",
            json={"provider": "remote", "model": "remote-large"},
        )
        unknown_provider = await client.post(
            "/api/ui/sessions/session-1/model",
            json={"provider": "nope", "model": "anything"},
        )
        unknown_model = await client.post(
            "/api/ui/sessions/session-1/model",
            json={"provider": "local", "model": "nope"},
        )
        missing_provider = await client.post(
            "/api/ui/sessions/session-1/model",
            json={"model": "remote-large"},
        )
        detached = await client.post(
            "/api/ui/sessions/session-1/model",
            json={"provider": "remote", "model": "remote-large"},
        )

    assert unknown_session.status_code == 404
    assert unknown_session.json()["error"]["code"] == "not_found"
    assert unknown_provider.status_code == 400
    assert unknown_provider.json()["error"]["message"] == "unknown provider: nope"
    assert unknown_model.status_code == 400
    assert unknown_model.json()["error"]["message"] == "unknown model: nope"
    assert missing_provider.status_code == 400
    assert missing_provider.json()["error"]["message"] == '"provider" must be a non-empty string'
    assert detached.status_code == 409
    assert detached.json()["error"]["code"] == "conflict"


# -- C. approval scopes ----------------------------------------------------


@dataclass
class FakeApprovalService:
    """The approve endpoints' slice of WebSessionHost."""

    settled: bool = True
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def pending_approvals(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return []

    def decide_approval(self, approval_id: str, decision: str, *, scope: str = "once") -> bool:
        self.calls.append((approval_id, decision, scope))
        return self.settled


async def test_approval_route_forwards_the_scope_and_echoes_it() -> None:
    service = FakeApprovalService()
    async with _client(service) as client:
        scoped = await client.post(
            "/api/ui/approvals/approval-1",
            json={"decision": "allow", "scope": "session"},
        )
        default = await client.post("/api/ui/approvals/approval-2", json={"decision": "deny"})

    assert scoped.status_code == 200
    assert scoped.json() == {
        "approval_id": "approval-1",
        "decision": "allow",
        "settled": True,
        "scope": "session",
    }
    assert default.status_code == 200
    assert default.json()["scope"] == "once"
    assert service.calls == [("approval-1", "allow", "session"), ("approval-2", "deny", "once")]


@pytest.mark.parametrize(
    "body",
    [
        {"decision": "allow", "scope": "forever"},
        {"decision": "allow", "scope": 5},
        {"decision": "allow", "scope": None},
    ],
)
async def test_approval_route_rejects_unknown_scopes(body: dict[str, Any]) -> None:
    service = FakeApprovalService()
    async with _client(service) as client:
        response = await client.post("/api/ui/approvals/approval-1", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert service.calls == []


async def test_approval_route_404_when_already_settled() -> None:
    service = FakeApprovalService(settled=False)
    async with _client(service) as client:
        response = await client.post(
            "/api/ui/approvals/approval-1",
            json={"decision": "allow", "scope": "session"},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert service.calls == [("approval-1", "allow", "session")]


async def test_host_decide_approval_forwards_the_scope(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path)
    _attach(host, tmp_path)
    request = ApprovalRequest(
        kind="sandbox-escalation",
        summary="Allow this write?",
        detail="the target file lives outside the workspace",
        tool="shell",
    )

    task = asyncio.create_task(host._ask_approval(request))
    prompt = (await _wait_for_prompts(host))[0]

    with pytest.raises(BadRequest, match="unknown approval decision"):
        host.decide_approval(prompt["approval_id"], "maybe")
    with pytest.raises(BadRequest, match="unknown approval scope"):
        host.decide_approval(prompt["approval_id"], "allow", scope="forever")
    assert host.pending_approvals() == [prompt]

    assert host.decide_approval(prompt["approval_id"], "allow", scope="session") is True
    assert (await task) is True
    # The session-scoped grant answers the same tool without another prompt.
    assert await host._ask_approval(request) is True
    assert host.pending_approvals() == []


# -- D. pending-turn queue -------------------------------------------------


async def test_send_message_enqueues_identifiable_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    binding = _attach(host, tmp_path)
    monkeypatch.setattr(host, "_start_turn_worker", lambda target: None)

    first = await host.send_message("session-1", SendMessageRequest(text="hello"))
    second = await host.send_message("session-1", SendMessageRequest(text="again"))

    assert first["queued"] == 0
    assert second["queued"] == 1
    snapshot = await host.queue("session-1")
    assert snapshot["session_id"] == "session-1"
    items = snapshot["items"]
    assert [item["text"] for item in items] == ["hello", "again"]
    assert items[0]["queue_id"] != items[1]["queue_id"]
    assert all(len(item["queue_id"]) == 32 for item in items)
    assert items[0]["created_at"] <= items[1]["created_at"]

    removed_id = items[0]["queue_id"]
    assert await host.drop_queued("session-1", removed_id) == {"ok": True, "removed": True}
    assert [turn.queue_id for turn in binding.pending] == [items[1]["queue_id"]]
    with pytest.raises(NotFound, match="was not found"):
        await host.drop_queued("session-1", removed_id)


async def test_queue_routes_expose_and_drop_waiting_turns(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path, infos=[_info("session-1", tmp_path)])
    binding = _attach(host, tmp_path)
    binding.pending.append(PendingTurn(queue_id="queued-two", text="second", created_at=2.0))
    binding.pending.append(PendingTurn(queue_id="queued-one", text="first", created_at=1.0))

    async with _client(host) as client:
        snapshot = await client.get("/api/ui/sessions/session-1/queue")
        removed = await client.delete("/api/ui/sessions/session-1/queue/queued-two")
        missing_turn = await client.delete("/api/ui/sessions/session-1/queue/queued-missing")
        missing_session = await client.get("/api/ui/sessions/session-missing/queue")
        invalid_session = await client.get("/api/ui/sessions/bad%20id/queue")

    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    assert snapshot.json() == {
        "session_id": "session-1",
        "items": [
            {"queue_id": "queued-two", "text": "second", "created_at": 2.0},
            {"queue_id": "queued-one", "text": "first", "created_at": 1.0},
        ],
    }
    assert removed.status_code == 200
    assert removed.json() == {"ok": True, "removed": True}
    assert missing_turn.status_code == 404
    assert missing_turn.json()["error"]["code"] == "not_found"
    assert missing_session.status_code == 404
    assert invalid_session.status_code == 400
    assert invalid_session.json()["error"]["code"] == "bad_request"
    assert [turn.queue_id for turn in binding.pending] == ["queued-one"]


# -- E. directory picker ---------------------------------------------------


def _make_tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "alpha").mkdir(parents=True)
    (root / "Beta").mkdir()
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("not-for-the-browser", encoding="utf-8")
    return root


async def test_fs_dirs_lists_only_visible_directories(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path)
    root = _make_tree(tmp_path)

    body = await host.fs_dirs(str(root))

    assert body["path"] == str(root.resolve())
    assert body["parent"] == str(root.resolve().parent)
    assert [entry["name"] for entry in body["entries"]] == ["alpha", "Beta"]
    for entry in body["entries"]:
        path = Path(entry["path"])
        assert path.is_absolute()
        assert path.is_dir()
        assert path.name == entry["name"]
    serialised = json.dumps(body)
    assert "notes.txt" not in serialised
    assert "not-for-the-browser" not in serialised


async def test_fs_dirs_defaults_to_workspace_then_server_cwd(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    host, _, _ = _host(tmp_path, infos=[_info("session-1", root)])
    _attach(host, root)

    assert (await host.fs_dirs())["path"] == str(root.resolve())

    host._binding = None
    assert (await host.fs_dirs(None))["path"] == str(tmp_path.resolve())


async def test_fs_dirs_errors_are_explicit(tmp_path: Path) -> None:
    host, _, _ = _host(tmp_path)
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")

    with pytest.raises(BadRequest, match="not a directory"):
        await host.fs_dirs(str(file_path))
    with pytest.raises(NotFound, match="does not exist"):
        await host.fs_dirs(str(tmp_path / "missing"))
    with pytest.raises(BadRequest, match="path must be absolute"):
        await host.fs_dirs("relative/path")


async def test_fs_roots_collects_home_workspace_and_recent_cwds(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    host, _, _ = _host(
        tmp_path,
        infos=[
            _info("session-1", tmp_path),
            _info("session-2", project),
            _info("session-3", tmp_path / "gone"),
        ],
    )

    body = await host.fs_roots()

    roots = body["roots"]
    assert roots[0] == {"label": "Home", "path": str(Path.home().resolve())}
    assert roots[1] == {"label": "Workspace", "path": str(tmp_path.resolve())}
    assert roots[2] == {"label": "project", "path": str(project.resolve())}
    assert str((tmp_path / "gone").resolve()) not in [root["path"] for root in roots]
    paths = [root["path"] for root in roots]
    assert len(paths) == len({path.casefold() for path in paths})


async def test_fs_roots_is_capped(tmp_path: Path) -> None:
    infos = []
    for index in range(FS_ROOTS_LIMIT + 5):
        directory = tmp_path / f"dir-{index:02d}"
        directory.mkdir()
        infos.append(_info(f"session-{index:03d}", directory))
    host, _, _ = _host(tmp_path, infos=infos)

    roots = (await host.fs_roots())["roots"]

    assert len(roots) == FS_ROOTS_LIMIT
    assert roots[0]["label"] == "Home"
    assert roots[1]["label"] == "Workspace"
    assert len({root["path"].casefold() for root in roots}) == FS_ROOTS_LIMIT


async def test_fs_routes_serve_the_directory_picker(tmp_path: Path) -> None:
    root = _make_tree(tmp_path)
    host, _, _ = _host(tmp_path)

    async with _client(host) as client:
        listing = await client.get("/api/ui/fs/dirs", params={"path": str(root)})
        default = await client.get("/api/ui/fs/dirs")
        roots = await client.get("/api/ui/fs/roots")
        file_target = await client.get("/api/ui/fs/dirs", params={"path": str(root / "notes.txt")})
        missing = await client.get("/api/ui/fs/dirs", params={"path": str(tmp_path / "missing")})
        relative = await client.get("/api/ui/fs/dirs", params={"path": "relative/path"})

    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "no-store"
    assert listing.json()["path"] == str(root.resolve())
    assert [entry["name"] for entry in listing.json()["entries"]] == ["alpha", "Beta"]
    assert "not-for-the-browser" not in listing.text
    assert default.status_code == 200
    assert default.json()["path"] == str(tmp_path.resolve())
    assert roots.status_code == 200
    assert roots.json()["roots"][1] == {"label": "Workspace", "path": str(tmp_path.resolve())}
    assert file_target.status_code == 400
    assert file_target.json()["error"]["code"] == "bad_request"
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert relative.status_code == 400
    assert relative.json()["error"]["code"] == "bad_request"
