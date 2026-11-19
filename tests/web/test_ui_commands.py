"""Tests for the UI-addressable command dispatcher (no browser required)."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.errors import ConfigError
from deeppyro.web.ui.commands import (
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REDIRECT,
    CommandResult,
    execute,
)


@dataclass
class FakePresets:
    ids: list[str] = field(default_factory=lambda: ["standard", "team"])
    default: Any = field(default_factory=lambda: SimpleNamespace(id="standard"))


@dataclass
class FakePermissions:
    _names: tuple[str, ...] = ("read-only", "workspace-write", "danger-full-access")

    def names(self) -> tuple[str, ...]:
        return self._names

    def apply(self, session: object, name: str) -> str:
        return name


@dataclass
class FakeConfig:
    agent: Any = field(default_factory=lambda: SimpleNamespace(provider="deepseek", model="deepseek-flash"))
    providers: dict[str, Any] = field(
        default_factory=lambda: {"deepseek": SimpleNamespace(models=[SimpleNamespace(id="deepseek-flash")])}
    )


@dataclass
class FakeService:
    active_session_id: str | None = "session-1"
    sessions: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "session_id": "session-1",
                "title": "active session",
                "turns": 2,
                "active": True,
                "archived": False,
            },
            {
                "session_id": "session-2",
                "title": "older session",
                "turns": 1,
                "active": False,
                "archived": False,
            },
        ]
    )
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    create_error: Exception | None = None
    services: Any = field(
        default_factory=lambda: SimpleNamespace(
            config=FakeConfig(),
            permission_presets=FakePermissions(),
            presets=FakePresets(),
        )
    )

    async def list_sessions(self, *, include_archived: bool = False) -> dict[str, Any]:
        rows = list(self.sessions)
        if not include_archived:
            rows = [row for row in rows if not row["archived"]]
        return {"sessions": rows, "active_session_id": self.active_session_id}

    async def create_session(self, request: Any) -> dict[str, Any]:
        if self.create_error is not None:
            raise self.create_error
        self.calls.append(("create", request.title or ""))
        self.created.append({"cwd": request.cwd or "", "title": request.title or ""})
        return {
            "session_id": "session-new",
            "title": request.title or "",
            "resumed": False,
            "cwd": request.cwd or "D:/work",
        }

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("resume", session_id))
        self.active_session_id = session_id
        return {"session_id": session_id, "resumed": True}

    async def rename_session(self, session_id: str, title: str) -> dict[str, Any]:
        self.calls.append(("rename", session_id, title))
        return {"session_id": session_id, "title": title, "active": True}

    async def archive_session(self, session_id: str, *, archived: bool = True) -> dict[str, Any]:
        self.calls.append(("archive", session_id, archived))
        if session_id == self.active_session_id:
            self.active_session_id = None
        return {"session_id": session_id, "archived": archived, "detached": True}

    async def apply_permission(self, session_id: str, preset: str) -> dict[str, Any]:
        self.calls.append(("permission", session_id, preset))
        return {"session_id": session_id, "permission_preset": preset}

    async def status(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "title": "active session",
            "provider": "deepseek",
            "model": "deepseek-flash",
            "preset": "standard",
            "permission_preset": "workspace-write",
            "cwd": "D:/work",
            "context_tokens": 10,
            "context_window": 100,
            "busy": False,
        }

    async def facts(self, session_id: str) -> dict[str, Any]:
        return {
            "info": {"session_id": session_id},
            "state": {
                "plan_mode": "off",
                "rounds_started": 3,
                "todos": [{"content": "ship it", "status": "in_progress"}],
                "goal": {
                    "id": "goal-1",
                    "revision": 1,
                    "objective": "Ship P1",
                    "phase": "active",
                    "max_goal_rounds": 10,
                    "blocked_reason": None,
                },
            },
        }


@dataclass
class FakeApp:
    rows: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"id": "kernel", "state": "active", "group": False},
            {"id": "llm", "state": "active", "group": False},
        ]
    )

    def row_states(self) -> list[dict[str, Any]]:
        return self.rows


def context(app: Any = None) -> Any:
    return SimpleNamespace(kernel_app=app)


@pytest.mark.asyncio
async def test_new_starts_a_session_and_returns_its_id() -> None:
    service = FakeService()
    result = await execute(service, context(), "new", ["a", "title"])
    assert isinstance(result, CommandResult)
    assert result.status == STATUS_OK
    assert result.data["session_id"] == "session-new"
    assert service.calls == [("create", "a title")]


@pytest.mark.asyncio
async def test_cwd_without_arguments_reports_the_current_workspace() -> None:
    service = FakeService()
    result = await execute(service, context(), "cwd")
    assert result.status == STATUS_OK
    assert result.data["cwd"] == "D:/work"
    assert service.calls == []


@pytest.mark.asyncio
async def test_cwd_with_a_path_starts_a_session_there() -> None:
    service = FakeService()
    result = await execute(service, context(), "cwd", ["D:/projects/other"])
    assert result.status == STATUS_OK
    assert result.data["session_id"] == "session-new"
    assert result.data["cwd"] == "D:/projects/other"
    assert "previous session is still in the sidebar" in result.message
    assert service.created[-1] == {"cwd": "D:/projects/other", "title": ""}


@pytest.mark.asyncio
async def test_cwd_reports_a_missing_directory_as_an_error() -> None:
    service = FakeService(create_error=ConfigError("workspace directory does not exist: D:/nope"))
    result = await execute(service, context(), "cwd", ["D:/nope"])
    assert result.status == STATUS_ERROR
    assert "workspace directory does not exist" in result.message


@pytest.mark.asyncio
async def test_resume_accepts_an_index() -> None:
    service = FakeService()
    result = await execute(service, context(), "/resume", ["2"])
    assert result.status == STATUS_OK
    assert result.data["session_id"] == "session-2"


@pytest.mark.asyncio
async def test_resume_accepts_a_title_substring() -> None:
    service = FakeService()
    result = await execute(service, context(), "resume", ["older"])
    assert result.data["session_id"] == "session-2"


@pytest.mark.asyncio
async def test_archive_of_the_active_session_opens_a_new_one() -> None:
    service = FakeService()
    result = await execute(service, context(), "archive")
    assert result.status == STATUS_OK
    assert result.data["session_id"] == "session-new"
    assert ("create", "") in service.calls


@pytest.mark.asyncio
async def test_permission_lists_presets_without_arguments() -> None:
    service = FakeService()
    result = await execute(service, context(), "permission")
    assert result.status == STATUS_OK
    assert result.data["presets"] == ["read-only", "workspace-write", "danger-full-access"]
    assert result.data["current"] == "workspace-write"


@pytest.mark.asyncio
async def test_permission_applies_a_named_preset() -> None:
    service = FakeService()
    result = await execute(service, context(), "permission", ["read-only"])
    assert result.data["permission_preset"] == "read-only"
    assert ("permission", "session-1", "read-only") in service.calls


@pytest.mark.asyncio
async def test_settings_redirects_to_a_known_section() -> None:
    result = await execute(FakeService(), context(), "settings", ["permissions"])
    assert result.status == STATUS_REDIRECT
    assert result.redirect == "/settings/permissions"


@pytest.mark.asyncio
async def test_unknown_commands_report_an_error_without_raising() -> None:
    result = await execute(FakeService(), context(), "nope")
    assert result.status == STATUS_ERROR
    assert "unknown command" in result.message


@pytest.mark.asyncio
async def test_unwired_commands_say_so() -> None:
    for name in ("compact", "jobs", "team", "thinking", "hooks", "exit"):
        result = await execute(FakeService(), context(), name)
        assert result.status == STATUS_ERROR
        assert result.message


@pytest.mark.asyncio
async def test_plugins_lists_booted_rows() -> None:
    result = await execute(FakeService(), context(FakeApp()), "plugins")
    assert result.status == STATUS_OK
    assert [row["id"] for row in result.data["plugins"]] == ["kernel", "llm"]
    assert "2 of 2 plugins active" in result.message


@pytest.mark.asyncio
async def test_help_lists_the_core_registry() -> None:
    result = await execute(FakeService(), context(), "help")
    assert result.status == STATUS_OK
    names = [row["name"] for row in result.data["commands"]]
    assert "new" in names and "settings" in names


@pytest.mark.asyncio
async def test_status_reports_context_and_rounds() -> None:
    result = await execute(FakeService(), context(), "status")
    assert result.status == STATUS_OK
    assert "10 / 100 tokens" in result.message
    assert "3 rounds" in result.message
