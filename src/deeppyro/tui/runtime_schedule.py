"""Layer 2 of the TUI runtime: scheduled sessions/agents and preset registries."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from deeppyro.agent.native_factory import AgentBuild
from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.api import DEFAULT_PRESET_ID
from deeppyro.api.services import service_name
from deeppyro.attachments.store import AttachmentStore
from deeppyro.builtin.inspect import InspectPlugin
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.plugin_manager import PluginManagerPlugin
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.schedule import SchedulePlugin
from deeppyro.builtin.settings import SettingsPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.core.errors import DeeppyroError
from deeppyro.hooks import HookService
from deeppyro.jobs.manager import JobsService
from deeppyro.llm.retry import RetryPolicy
from deeppyro.memory.service import MemoryService
from deeppyro.observability.protocols import TraceExportSink, UsageSink
from deeppyro.ptc.runtime import PtcRuntime
from deeppyro.sandbox.approval import ApprovalService
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.sandbox.policy import SandboxPolicyService
from deeppyro.session.native_control import NewSession
from deeppyro.subagent.manager import SubagentService
from deeppyro.team.manager import TeamService
from deeppyro.tui.runtime_services import ServiceLayer
from deeppyro.tui.runtime_support import await_maybe, logger
from deeppyro.user_questions.service import UserQuestionsService
from deeppyro.working_directory.service import WorkingDirectoryService

_TOOL_CONTEXT_SERVICES: tuple[type[Any], ...] = (
    LlmPlugin,
    HookService,
    RetryPolicy,
    SandboxPlugin,
    SandboxPolicyService,
    ApprovalService,
    PermissionPresetsService,
    PluginManagerPlugin,
    InspectPlugin,
    SettingsPlugin,
    JobsService,
    TeamService,
    SubagentService,
    WorkingDirectoryService,
    UserQuestionsService,
    AttachmentStore,
    SchedulePlugin,
    MemoryService,
    PtcRuntime,
)


class ScheduleLayer(ServiceLayer):
    """Layer 2 of the TUI runtime: scheduled sessions/agents and preset registries."""

    async def create_schedule_session(self, spec: Any, preset: str) -> Any:
        """Create a schema 1 headless session for a schedule delivery."""
        directory = str(getattr(spec, "cwd", "") or "")
        cwd = self.cwd
        if directory:
            candidate = Path(directory).expanduser()
            try:
                candidate = candidate.resolve()
            except OSError:
                candidate = self.cwd
            if candidate.is_dir():
                cwd = candidate
        handle = NativeSessionHandle(
            self.sessions.native_create(NewSession(cwd=Path(cwd), preset=preset, origin="headless"))
        )
        self._pin_permission(handle)
        await self._session_created(handle, cwd=str(cwd), preset=preset)
        return handle

    async def create_schedule_agent(self, session: Any, *, preset: str) -> Any:
        """Build a silent agent for a scheduled turn."""
        await self._ensure_preset(preset, None)
        handle = (
            session if getattr(session, "native_session", None) is not None else NativeSessionHandle(session)
        )
        build = AgentBuild(
            provider=self.provider_name,
            model=self.model,
            cwd=Path(self.cwd),
            preset=preset,
            permission=self.permission,
            hooks=None,
            prompt_facts=self._prompt_facts(handle),
            services=self._plugin_services(),
            context_window=self.context_window,
        )
        return self.agents.host(handle.native_session, build)

    def track_schedule_agent(self, session: Any, agent: Any) -> None:
        """Close a background agent once its scheduled turn is idle."""
        task = asyncio.create_task(self._finish_schedule_agent(session, agent))
        self._schedule_tasks.add(task)
        task.add_done_callback(self._schedule_tasks.discard)

    async def _finish_schedule_agent(self, session: Any, agent: Any) -> None:
        try:
            wait = getattr(session, "wait_for_idle", None)
            if not callable(wait):
                wait = getattr(agent, "wait_for_idle", None)
            if callable(wait):
                await await_maybe(wait())
        except Exception:  # noqa: BLE001 - background cleanup is best effort
            pass
        finally:
            close = getattr(agent, "aclose", None)
            if callable(close):
                # background cleanup is best effort
                with contextlib.suppress(Exception):
                    await await_maybe(close())

    def _canonical_preset(self, preset: Any) -> str | None:
        """Normalize a preset id through the presets service aliases."""
        value = str(preset or "").strip()
        if not value:
            return None
        service = self._service(PresetsPlugin)
        canonical = getattr(service, "canonical", None)
        if callable(canonical):
            try:
                normalized = canonical(value)
            except Exception:  # noqa: BLE001 - normalization is best effort
                return value
            return str(normalized).strip() if normalized else None
        return value

    def _plugin_services(self) -> dict[str, Any]:
        """Plugin services the agent tools read from ToolContext.services.

        ToolContext.services is a string-keyed mapping by contract (tools receive
        it as a mapping of service names), so each typed contract is resolved and
        stored under its registered service name.
        """
        services: dict[str, Any] = {}
        for contract in _TOOL_CONTEXT_SERVICES:
            service = self._service(contract)
            if service is not None:
                services[service_name(contract)] = service
        for contract in (TraceExportSink, UsageSink):
            service = self._service(contract)
            if service is not None:
                services[service_name(contract)] = service
        return services

    def start_schedule_send(self, agent: Any, message: str, source: dict[str, Any]) -> None:
        """Send a scheduled message through a native agent in the background."""
        send = getattr(agent, "send", None)
        if not callable(send):
            raise DeeppyroError("native schedule agent cannot send messages")

        async def _send() -> None:
            try:
                await await_maybe(send(message, source=source))
            except Exception:  # noqa: BLE001 - background delivery must not crash the TUI
                logger.warning("scheduled native turn failed", exc_info=True)

        task = asyncio.create_task(_send())
        self._schedule_tasks.add(task)
        task.add_done_callback(self._schedule_tasks.discard)

    def _scoped_registry(self, workspace: Any) -> Any:
        """Tools registry narrowed to contributions visible to the preset."""
        service = self._service(ToolsPlugin)
        if service is None:
            return self.registry
        registry_for = getattr(service, "registry_for", None)
        if registry_for is None:
            return self.registry
        scope = self._canonical_preset(self.agent_preset) or DEFAULT_PRESET_ID
        try:
            return registry_for(Path(workspace), scope=scope)
        except TypeError:
            return registry_for(Path(workspace))

    async def _ensure_preset(self, preset_id: str, revision: int | None) -> None:
        """Materialize one preset revision (no-op without a presets service)."""
        service = self._service(PresetsPlugin)
        ensure = getattr(service, "ensure", None)
        if callable(ensure):
            canonical = self._canonical_preset(preset_id) or preset_id
            await await_maybe(ensure(canonical, revision=revision))
