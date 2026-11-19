"""One Python seam between the NiceGUI pages and the native web host.

The browser UI never talks HTTP to its own process. Every action a page can
take goes through this bridge, which narrows WebSessionHost to plain dictionaries
and translates the few error types into messages a page can render. The JSON API
in api.py stays available for tests and non-browser clients, but it is no
longer the path the first-party UI uses.

The bridge is deliberately stateless: one instance can serve many browser
clients, and the session host already owns the only mutable state (the attached
session, its turn queue and the approval broker).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from deeppyro.builtin.commands_builtin import core_command_specs
from deeppyro.builtin.settings import SettingsPlugin
from deeppyro.core.errors import ConfigError
from deeppyro.plugins.errors import PluginError, SettingsConflict
from deeppyro.web.ui.api import COMMAND_GROUPS, UiContext
from deeppyro.web.ui.commands import CommandResult, execute
from deeppyro.web.ui.settings import ConfigEditor, settings_payload, test_provider
from deeppyro.web.wire import (
    CreateSessionRequest,
    HttpError,
    SendMessageRequest,
    ServiceUnavailable,
)


@dataclass(frozen=True, slots=True)
class BridgeResult:
    """A small, page-safe result for one bridge action."""

    ok: bool
    message: str = ""
    data: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WebUiBridge:
    """The complete read/write surface the NiceGUI pages are allowed to use."""

    service: Any
    context: UiContext

    # -- identity and catalogue -------------------------------------------

    @property
    def version(self) -> str:
        return self.context.version

    @property
    def active_session_id(self) -> str | None:
        return getattr(self.service, "active_session_id", None)

    @property
    def default_cwd(self) -> str:
        return str(getattr(self.service, "default_cwd", "") or "")

    @property
    def services(self) -> Any:
        return getattr(self.service, "services", None)

    @property
    def kernel_app(self) -> Any:
        return self.context.kernel_app

    async def bootstrap(self) -> dict[str, Any]:
        """Everything the shell needs for its first paint."""
        health = await self.service.health()
        sessions = await self.service.list_sessions()
        return {
            "version": self.context.version,
            "health": health,
            "sessions": sessions.get("sessions", []),
            "active_session_id": self.service.active_session_id,
            "commands": self.commands(),
            "presets": self.presets(),
            "permissions": self.permissions(),
            "plugins": self.plugins(),
            "providers": self.providers(),
            "defaults": self.defaults(),
        }

    async def sessions(self, *, include_archived: bool = False) -> dict[str, Any]:
        return await self.service.list_sessions(include_archived=include_archived)

    def commands(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in core_command_specs():
            rows.append(
                {
                    "name": spec.name,
                    "aliases": list(spec.aliases),
                    "summary": spec.summary,
                    "usage": spec.usage,
                    "group": COMMAND_GROUPS.get(spec.name, "system"),
                    "takes_arg": "[" in spec.usage,
                }
            )
        return rows

    def presets(self) -> list[dict[str, Any]]:
        try:
            specs = self.service.services.presets.all()
        except Exception:  # noqa: BLE001 - catalogue is advisory
            return []
        return [
            {
                "id": spec.id,
                "name": spec.name,
                "description": spec.description,
                "model": spec.model,
                "mode": str(spec.mode),
                "hidden": bool(spec.hidden),
                "order": spec.order,
            }
            for spec in specs
            if not spec.hidden
        ]

    def permissions(self) -> list[dict[str, Any]]:
        try:
            specs = self.service.services.permission_presets.catalog()
        except Exception:  # noqa: BLE001
            return []
        return [
            {
                "name": spec.name,
                "sandbox": str(spec.sandbox),
                "approval": str(spec.approval),
                "description": spec.description,
            }
            for spec in specs
        ]

    def plugins(self) -> list[dict[str, Any]]:
        app = self.context.kernel_app
        if app is None or not hasattr(app, "row_states"):
            return []
        try:
            rows = app.row_states()
        except Exception:  # noqa: BLE001
            return []
        rendered: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                rendered.append(
                    {
                        "id": str(row.get("id", "")),
                        "plugin_id": str(row.get("plugin_id", row.get("id", ""))),
                        "state": str(row.get("state", "")),
                        "version": str(row.get("version", "")),
                        "description": str(row.get("description", "")),
                        "group": str(row.get("group", "")),
                    }
                )
        return rendered

    def providers(self) -> list[dict[str, Any]]:
        config = getattr(self.service.services, "config", None)
        if config is None:
            return []
        selected = config.agent.provider
        rendered: list[dict[str, Any]] = []
        for name, provider in config.providers.items():
            rendered.append(
                {
                    "id": name,
                    "api": provider.api,
                    "base_url": provider.base_url,
                    "api_key_env": provider.api_key_env,
                    "configured": bool(provider.api_key or provider.api_key_env),
                    "selected": name == selected,
                    "models": [
                        {
                            "id": model.id,
                            "name": model.name or model.id,
                            "context_window": model.context_window,
                        }
                        for model in provider.models
                    ],
                }
            )
        return rendered

    def defaults(self) -> dict[str, Any]:
        config = getattr(self.service.services, "config", None)
        if config is None:
            return {}
        agent = config.agent
        return {
            "provider": agent.provider,
            "model": agent.model,
            "temperature": agent.temperature,
            "max_tokens": agent.max_tokens,
            "max_steps": agent.max_steps,
            "cwd": self.default_cwd,
        }

    # -- sessions ----------------------------------------------------------

    async def timeline(self, session_id: str | None) -> dict[str, Any]:
        """A transcript plus live status; errors become data, never a 500 page."""
        if not session_id:
            return {"error": "no session is selected", "messages": [], "readable": False}
        try:
            payload = await self.service.timeline(session_id)
        except HttpError as exc:
            return {
                "session_id": session_id,
                "error": exc.message,
                "messages": [],
                "readable": False,
                "active": False,
            }
        journal = getattr(self.service, "journal", None)
        if journal is not None:
            payload["journal_seq"] = journal.last_seq
        return payload

    async def facts(self, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            return {}
        try:
            return await self.service.facts(session_id)
        except HttpError:
            return {}

    async def create(
        self,
        *,
        cwd: str | None = None,
        title: str | None = None,
        preset: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        return await self.service.create_session(
            CreateSessionRequest(
                cwd=cwd or None,
                preset=preset or None,
                provider=provider or None,
                model=model or None,
                title=title or None,
            )
        )

    async def resume(self, session_id: str) -> dict[str, Any]:
        return await self.service.resume_session(session_id)

    async def rename(self, session_id: str, title: str) -> dict[str, Any]:
        return await self.service.rename_session(session_id, title)

    async def archive(self, session_id: str, *, archived: bool = True) -> dict[str, Any]:
        return await self.service.archive_session(session_id, archived=archived)

    async def permission(self, session_id: str, preset: str) -> dict[str, Any]:
        return await self.service.apply_permission(session_id, preset)

    # -- turns -------------------------------------------------------------

    async def send(self, session_id: str, text: str) -> dict[str, Any]:
        return await self.service.send_message(session_id, SendMessageRequest(text=text))

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        return await self.service.interrupt(session_id)

    async def approvals(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return list(self.service.pending_approvals(session_id))

    def decide_approval(self, approval_id: str, decision: str) -> bool:
        return bool(self.service.decide_approval(approval_id, decision))

    async def sse(self, session_id: str, *, after: int = 0) -> AsyncIterator[bytes]:
        async for frame in self.service.sse(session_id, after=after):
            yield frame

    # -- commands ----------------------------------------------------------

    async def execute(
        self,
        name: str,
        args: list[str] | None = None,
        session_id: str | None = None,
    ) -> CommandResult:
        return await execute(self.service, self.context, name, args or [], session_id)

    # -- settings ----------------------------------------------------------

    def editor(self) -> ConfigEditor:
        return ConfigEditor()

    async def settings(self) -> dict[str, Any]:
        return settings_payload(self.context.kernel_app, self.editor(), self.service, self.version)

    async def update_agent(self, **changes: Any) -> dict[str, Any]:
        return self.editor().update_agent(**changes)

    async def update_provider(self, provider: str, **changes: Any) -> dict[str, Any]:
        return self.editor().update_provider(provider, **changes)

    async def test_provider(self, provider: str) -> dict[str, Any]:
        llm = getattr(self.service.services, "llm", None)
        if llm is None:
            raise ServiceUnavailable("llm service is not available")
        return await test_provider(self.editor(), provider, llm)

    async def set_permission(self, preset: str) -> dict[str, Any]:
        return self.editor().set_permission_default(preset)

    def settings_store(self) -> Any:
        app = self.context.kernel_app
        if app is None:
            raise ServiceUnavailable("the plugin host is not available on this surface")
        try:
            return app.require(SettingsPlugin)
        except Exception as exc:
            raise ServiceUnavailable("the settings plugin is not running") from exc

    def plugin_settings(self, plugin_id: str) -> dict[str, Any]:
        return self.settings_store().describe(plugin_id)

    def update_plugin_settings(
        self,
        plugin_id: str,
        values: Mapping[str, Any],
        expected_revision: str = "",
    ) -> dict[str, Any]:
        store = self.settings_store()
        described = store.describe(plugin_id)
        expected = expected_revision or str(described.get("revision", ""))
        return store.write(plugin_id, dict(values), expected)


def bridge_error(exc: BaseException) -> str:
    """Turn an expected bridge exception into a sentence a page can show."""
    if isinstance(exc, (HttpError, ConfigError, PluginError, SettingsConflict, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


__all__ = ["BridgeResult", "WebUiBridge", "bridge_error"]
