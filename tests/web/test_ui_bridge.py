"""The bridge is the only seam between the NiceGUI pages and the host."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from deeppyro.web.ui.api import UiContext
from deeppyro.web.ui.bridge import WebUiBridge, bridge_error
from deeppyro.web.wire import CreateSessionRequest, NotFound, SendMessageRequest


class _Journal:
    last_seq = 17


class _Service:
    def __init__(self) -> None:
        self.active_session_id: str | None = "session-a"
        self.default_cwd = "D:/work"
        self.journal = _Journal()
        self.services = SimpleNamespace(
            config=SimpleNamespace(
                agent=SimpleNamespace(
                    provider="deepseek",
                    model="deepseek-flash",
                    temperature=0.2,
                    max_tokens=4096,
                    max_steps=12,
                ),
                providers={
                    "deepseek": SimpleNamespace(
                        api="openai-completions",
                        base_url="https://api.deepseek.com/v1",
                        api_key="sk-secret",
                        api_key_env=None,
                        models=[SimpleNamespace(id="deepseek-flash", name="Flash", context_window=65536)],
                    )
                },
            )
        )
        self.created: list[CreateSessionRequest] = []
        self.sent: list[tuple[str, SendMessageRequest]] = []
        self.decisions: list[tuple[str, str]] = []
        self.timeline_error: Exception | None = None

    async def health(self) -> dict[str, Any]:
        return {"ok": True, "version": "0.3.0"}

    async def list_sessions(self, *, include_archived: bool = False) -> dict[str, Any]:
        return {"sessions": [{"session_id": "session-a"}], "active_session_id": self.active_session_id}

    async def timeline(self, session_id: str) -> dict[str, Any]:
        if self.timeline_error is not None:
            raise self.timeline_error
        return {"session_id": session_id, "messages": [{"id": "m1", "role": "user"}], "readable": True}

    async def facts(self, session_id: str) -> dict[str, Any]:
        return {"info": {"session_id": session_id}, "state": {"title": "A"}}

    async def create_session(self, request: CreateSessionRequest) -> dict[str, Any]:
        self.created.append(request)
        return {"session_id": "session-new", "resumed": False, "cwd": request.cwd}

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        return {"session_id": session_id, "resumed": True}

    async def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        return {"session_id": session_id, "title": title}

    async def archive_session(self, session_id: str, *, archived: bool = True) -> dict[str, Any]:
        return {"session_id": session_id, "archived": archived}

    async def apply_permission(self, session_id: str, preset: str) -> dict[str, Any]:
        return {"session_id": session_id, "permission_preset": preset}

    async def send_message(self, session_id: str, request: SendMessageRequest) -> dict[str, Any]:
        self.sent.append((session_id, request))
        return {"accepted": True, "queued": 0}

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        return {"accepted": True}

    def pending_approvals(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [{"approval_id": "approval-1", "session_id": session_id or "session-a"}]

    def decide_approval(self, approval_id: str, decision: str) -> bool:
        self.decisions.append((approval_id, decision))
        return True


def _bridge(service: _Service | None = None) -> tuple[WebUiBridge, _Service]:
    host = service or _Service()
    return WebUiBridge(host, UiContext(token="tok", version="0.3.0")), host


async def test_bootstrap_projects_the_shell_catalogue() -> None:
    bridge, _host = _bridge()
    payload = await bridge.bootstrap()

    assert payload["version"] == "0.3.0"
    assert payload["active_session_id"] == "session-a"
    assert payload["sessions"] == [{"session_id": "session-a"}]
    assert len(payload["commands"]) == 25
    assert payload["commands"][0]["group"] in {"session", "model", "work", "system"}
    assert payload["providers"][0]["configured"] is True
    assert payload["providers"][0]["models"][0]["context_window"] == 65536
    assert payload["defaults"]["cwd"] == "D:/work"


async def test_timeline_adds_the_journal_cursor_and_turns_errors_into_data() -> None:
    bridge, host = _bridge()
    payload = await bridge.timeline("session-a")
    assert payload["journal_seq"] == 17
    assert payload["messages"][0]["role"] == "user"

    host.timeline_error = NotFound("unknown session")
    failed = await bridge.timeline("session-a")
    assert failed["error"] == "unknown session"
    assert failed["readable"] is False
    assert failed["messages"] == []


async def test_create_and_send_keep_the_typed_request_boundary() -> None:
    bridge, host = _bridge()
    created = await bridge.create(cwd="D:/other", title="Plan", preset="standard")
    assert created["session_id"] == "session-new"
    assert host.created[0].cwd == "D:/other"
    assert host.created[0].title == "Plan"
    assert host.created[0].preset == "standard"

    await bridge.send("session-a", "hello")
    assert host.sent[0][0] == "session-a"
    assert host.sent[0][1].text == "hello"


async def test_commands_and_approvals_use_the_shared_dispatchers() -> None:
    bridge, host = _bridge()
    help_result = await bridge.execute("/help")
    assert help_result.status == "ok"
    assert help_result.data["commands"]

    unknown = await bridge.execute("nope")
    assert unknown.status == "error"

    assert await bridge.approvals("session-a") == [{"approval_id": "approval-1", "session_id": "session-a"}]
    assert bridge.decide_approval("approval-1", "allow") is True
    assert host.decisions == [("approval-1", "allow")]


def test_bridge_error_only_leaks_expected_messages() -> None:
    assert bridge_error(NotFound("missing")) == "missing"
    assert bridge_error(ValueError("bad value")) == "bad value"
    assert bridge_error(RuntimeError("boom")) == "RuntimeError: boom"
