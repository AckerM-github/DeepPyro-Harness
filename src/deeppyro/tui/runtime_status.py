"""Layer 7 of the TUI runtime: permissions, workspaces, and session status."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.tui.runtime_catalog import CatalogLayer
from deeppyro.tui.runtime_support import await_maybe, logger, resolve_workspace


class StatusLayer(CatalogLayer):
    """Layer 7 of the TUI runtime: permissions, workspaces, and session status."""

    def _validate_model(self, provider_name: str, model_id: str) -> None:
        """Reject models the config does not know and the catalog has not seen."""
        available = self.models(provider_name)
        if not available or model_id in available:
            return
        if self._cached_model(provider_name, model_id) is not None:
            return
        raise ConfigError(
            f"unknown model: {model_id} (provider {provider_name} offers {', '.join(available)})"
        )

    def permission_preset(self) -> str:
        """The current named permission preset (or "custom")."""
        service = self._service(PermissionPresetsService)
        current = getattr(service, "current", None)
        if callable(current) and self.session is not None:
            try:
                return str(current(self.session))
            except Exception:  # noqa: BLE001 - an optional service must not break the UI
                logger.debug("permission preset unavailable", exc_info=True)
        return str(getattr(self.permission, "preset", "workspace-write"))

    async def set_permission(self, preset: str) -> str:
        """Switch the named permission preset for the live session."""
        service = self._service(PermissionPresetsService)
        apply = getattr(service, "apply", None)
        if not callable(apply) or self.session is None:
            raise DeeppyroError("permission presets are not available in this session")
        return str(apply(self.session, preset))

    async def set_workspace(self, path: str | Path) -> Path:
        """Rebind tools and approvals to a new workspace, keeping the session."""
        workspace = resolve_workspace(path)
        if workspace == self.cwd:
            return workspace
        kernel_app = self.kernel_app
        if kernel_app is None:
            raise DeeppyroError("cannot switch workspace after the kernel has stopped")
        gate = kernel_app.require(SandboxPlugin).gate(workspace, ask=self.ask, session=self.session)
        registry = kernel_app.require(ToolsPlugin).registry_for(workspace)
        self.permission = gate
        self.registry = registry
        self.cwd = workspace
        self._record("working-directory/change", {"cwd": str(workspace)})
        if self.session is not None:
            await self._attach(self.session)
        return workspace

    def known_workspaces(self, limit: int = 12) -> list[str]:
        """Workspace paths remembered by previous sessions, newest first."""
        paths: list[str] = []
        try:
            infos = self.store.list()
        except Exception:  # noqa: BLE001 - the dropdown must keep working
            infos = []
        for info in infos:
            cwd = str(getattr(info, "cwd", "") or "")
            if cwd and cwd not in paths:
                paths.append(cwd)
            if len(paths) >= max(limit, 1):
                break
        return paths

    async def ensure_presets(self) -> None:
        """Materialize every preset realm before any agent is created."""
        service = self._service(PresetsPlugin)
        ensure_all = getattr(service, "ensure_all", None)
        if callable(ensure_all):
            await await_maybe(ensure_all())

    @property
    def session_id(self) -> str:
        return str(getattr(self.session, "session_id", "") or "")

    def session_usage(self) -> dict[str, int]:
        """Cumulative token usage of the active session."""
        getter = getattr(self.session, "usage", None)
        if getter is None:
            return {"input": 0, "output": 0, "cached": 0}
        try:
            usage = getter()
        except Exception:  # noqa: BLE001 - usage display must never break the UI
            return {"input": 0, "output": 0, "cached": 0}
        return {key: int(usage.get(key, 0)) for key in ("input", "output", "cached")}

    def context_tokens(self) -> int:
        """Prompt tokens of the latest request, i.e. the live context size."""
        getter = getattr(self.session, "context_tokens", None)
        if getter is None:
            return self.session_usage()["input"]
        try:
            return int(getter())
        except Exception:  # noqa: BLE001 - context display must never break the UI
            return 0

    def recent_sessions(self, limit: int = 20, *, include_archived: bool = False) -> list[Any]:
        """Most recent sessions, newest first; archived ones are hidden by default."""
        return list(self.store.list(include_archived=include_archived))[: max(limit, 0)]

    def archived_sessions(self, limit: int = 20) -> list[Any]:
        """Archived sessions only, newest first."""
        sessions = [info for info in self.store.list(include_archived=True) if info.archived]
        return sessions[: max(limit, 0)]

    async def compact_now(self) -> bool:
        """Compact the active session immediately; True when it shrank."""
        session, agent = self.session, self.agent
        if session is None or agent is None:
            return False
        compactor = getattr(agent, "compactor", None)
        if compactor is None:
            raise DeeppyroError("compaction is not available for this agent")
        messages = session.derive_messages()
        return bool(await compactor.compact(session, messages))
