"""Layer 7 of the TUI runtime: permissions, workspaces, and session status."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.sandbox.permission import PermissionPresetsService
from deeppyro.session.native_index import NativeSessionInfo
from deeppyro.tui.native_facts import native_view
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
        derived = self._native_permission_preset()
        if derived is not None:
            return derived
        return str(getattr(self.permission, "preset", "workspace-write"))

    async def set_permission(self, preset: str) -> str:
        """Switch the named permission preset for the live session."""
        return await self._set_permission_native(preset)

    async def set_workspace(self, path: str | Path) -> Path:
        """Rebind tools and approvals to a new workspace, keeping the session."""
        workspace = resolve_workspace(path)
        if workspace == self.cwd:
            return workspace
        kernel_app = self.kernel_app
        if kernel_app is None:
            raise DeeppyroError("cannot switch workspace after the kernel has stopped")
        bound = native_view(self.session)
        gate = kernel_app.require(SandboxPlugin).gate(workspace, ask=self.ask, session=bound)
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
            infos = self._native_infos()
        except Exception:  # noqa: BLE001 - the dropdown must keep working
            infos = []
        for info in infos:
            cwd = str(getattr(info, "cwd", "") or "")
            if cwd and cwd not in paths:
                paths.append(cwd)
            if len(paths) >= max(limit, 1):
                break
        return paths

    def _native_permission_preset(self) -> str | None:
        """Derive the named preset from the sandbox and approval facts."""
        state = self._native_state()
        if state is None:
            return None
        sandbox_mode = getattr(state, "sandbox_mode", None)
        approval = getattr(state, "permission_policy", None)
        if sandbox_mode is None and approval is None:
            return None
        service = self._service(PermissionPresetsService)
        catalog = getattr(service, "catalog", None)
        if callable(catalog):
            try:
                for spec in cast("Iterable[Any]", catalog()):
                    if spec.sandbox == sandbox_mode and spec.approval == approval:
                        return str(spec.name)
            except Exception:  # noqa: BLE001 - the status line must never fail
                logger.debug("permission preset catalog unavailable", exc_info=True)
        return "custom"

    def _native_state(self) -> Any:
        """Folded session state of the live native session (None when absent)."""
        view = native_view(self.session)
        fold = getattr(view, "fold", None)
        if not callable(fold):
            return None
        try:
            folded = fold()
            return getattr(folded, "state", None)
        except Exception:  # noqa: BLE001 - status must never fail
            return None

    def _native_infos(self, *, include_archived: bool = False, limit: int | None = None) -> list[Any]:
        """Native session infos through the sessions plugin (empty when unavailable)."""
        sessions = self.sessions
        lister = getattr(sessions, "native_list", None)
        if not callable(lister):
            return []
        try:
            entries = list(cast("Iterable[Any]", lister(include_archived=include_archived, limit=limit)))
        except TypeError:
            entries = list(cast("Iterable[Any]", lister(include_archived=include_archived)))
        except Exception:  # noqa: BLE001 - the session browser must survive a bad directory
            logger.debug("native session listing failed", exc_info=True)
            return []
        infos = [info for info in entries if info is not None]
        if limit is not None:
            return infos[: max(limit, 0)]
        return infos

    async def _set_permission_native(self, preset: str) -> str:
        """Record permission facts on the native session and rebuild the agent."""
        service = self._service(PermissionPresetsService)
        getter = getattr(service, "get", None)
        if not callable(getter) or self.session is None:
            raise DeeppyroError("permission presets are not available in this session")
        spec = cast(Any, getter(preset))
        session = self.session
        state = self._native_state()
        if getattr(state, "sandbox_mode", None) != spec.sandbox:
            session.append("sandbox/mode", {"mode": spec.sandbox})
        if getattr(state, "permission_policy", None) != spec.approval:
            session.append("approval/policy", {"policy": spec.approval})
        kernel_app = self.kernel_app
        if kernel_app is not None:
            gate = kernel_app.require(SandboxPlugin).gate(
                self.cwd,
                ask=self.ask,
                session=native_view(session),
            )
            self.permission = gate
            await self._attach(session)
        return str(spec.name)

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
        """Most recent native sessions, newest first; archived ones are hidden by default."""
        return self._native_infos(include_archived=include_archived, limit=max(limit, 0))

    def native_sessions(self, limit: int = 20) -> list[NativeSessionInfo]:
        """Schema 1 (native) sessions, newest first."""
        if limit <= 0:
            return []
        return [info for info in self.recent_sessions(limit) if isinstance(info, NativeSessionInfo)]

    def archived_sessions(self, limit: int = 20) -> list[Any]:
        """Archived native sessions only, newest first."""
        archived = [
            info for info in self._native_infos(include_archived=True) if getattr(info, "archived", False)
        ]
        return archived[: max(limit, 0)]

    async def compact_now(self) -> bool:
        """Compact the active native session immediately; True when it shrank."""
        return await self._compact_native()

    async def _compact_native(self) -> bool:
        """Compact a native session through the optional native compactor."""
        compactor = self.compactor
        if compactor is None:
            raise DeeppyroError("native compaction is not in this bundle; /compact is unavailable")
        session = self.session
        if session is None:
            return False
        fold = getattr(session, "fold", None)
        view = fold() if callable(fold) else None
        compact = getattr(compactor, "native_compact", None) or getattr(compactor, "compact", None)
        if not callable(compact):
            raise DeeppyroError("native compaction is not in this bundle; /compact is unavailable")
        result = await cast(Any, compact)(session, view=view)
        return result is not None
