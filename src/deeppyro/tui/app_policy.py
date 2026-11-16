"""Layer 7 of the DeepPyro TUI app: Permission and workspace policy command layer."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from deeppyro.tui.app_support import CUSTOM_WORKSPACE, CUSTOM_WORKSPACE_LABEL
from deeppyro.tui.app_turns import TurnLayer
from deeppyro.tui.screens.path_prompt import PathPromptScreen


class PolicyLayer(TurnLayer):
    """Permission and workspace policy command layer."""

    async def permission_command(self, argument: str) -> None:
        """Show or switch the permission preset (/permission)."""
        names_reader = getattr(self.runtime, "permission_names", None)
        if callable(names_reader):
            read: Callable[[], Any] = names_reader
            presets = list(read())
        else:
            presets = []
        if not presets:
            presets = ["read-only", "workspace-write", "danger-full-access"]
        if not argument:
            if self._working:
                self.notify("cannot change permission during a running turn", severity="warning")
                return
            current = self._current_permission()
            choice = await self._pick(
                "Select permission preset",
                [(item, item, "current" if item == current else "") for item in presets],
            )
            if choice is None:
                self.conversation.add_info(f"current permission preset: {current}")
                self.conversation.add_info("available: " + ", ".join(presets))
                self.conversation.add_info("use /permission <preset>")
                return
            argument = choice
        if argument not in presets:
            self.conversation.add_error(
                f"unknown permission preset: {argument} (choose from {', '.join(presets)})"
            )
            return
        await self._apply_permission(argument)

    async def cwd_command(self, argument: str) -> None:
        """Show or switch the workspace directory (/cwd)."""
        if self._working:
            self.notify("cannot switch workspace during a running turn", severity="warning")
            return
        runtime = self.runtime
        current = str(getattr(runtime, "cwd", "") or "")
        if not argument:
            known = getattr(runtime, "known_workspaces", None)
            if callable(known):
                read: Callable[[], Any] = known
                workspaces = list(read())
            else:
                workspaces = []
            options: list[tuple[str, str, str]] = []
            for path in [current, *workspaces]:
                if path and path not in [item[0] for item in options]:
                    options.append((path, path, "current" if path == current else ""))
            options.append((CUSTOM_WORKSPACE, CUSTOM_WORKSPACE_LABEL, ""))
            choice = await self._pick("Select workspace", options)
            if choice is None:
                self.conversation.add_info(f"current workspace: {current}")
                if workspaces:
                    self.conversation.add_info("recent: " + ", ".join(workspaces))
                self.conversation.add_info("use /cwd <path>")
                return
            argument = choice
        if argument == CUSTOM_WORKSPACE:
            path = await self._ask_workspace_path()
            if not path:
                return
            argument = path
        await self._apply_workspace(argument)

    async def _apply_permission(self, preset: str) -> None:
        if self._working:
            self.notify("cannot change permission during a running turn", severity="warning")
            self.refresh_status()
            return
        try:
            selected = await self.runtime.set_permission(preset)
        except Exception as exc:  # noqa: BLE001 - surface, then resync the view
            self.notify(f"permission switch failed: {exc}", severity="error")
        else:
            self.notify(f"permission preset: {selected}")
        finally:
            self.refresh_status()

    async def _apply_workspace(self, path: str | Path) -> None:
        if self._working:
            self.notify("cannot switch workspace during a running turn", severity="warning")
            self.refresh_status()
            return
        runtime = self.runtime
        try:
            workspace = await runtime.set_workspace(path)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"workspace switch failed: {exc}", severity="error")
        else:
            self.conversation.add_info(f"workspace switched to {workspace}")
            self.notify(f"cwd: {workspace}")
        finally:
            self.refresh_status()

    async def _ask_workspace_path(self) -> str | None:
        runtime = self.runtime
        current = str(getattr(runtime, "cwd", "") or self._cwd or "")
        try:
            return await self.push_screen_wait(PathPromptScreen(current))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            return None
