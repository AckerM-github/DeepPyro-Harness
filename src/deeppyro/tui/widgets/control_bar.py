"""Top control bar: permission preset, approval policy, and workspace selectors.

The bar is deliberately a pure view. It renders three Textual Select widgets,
exposes sync() so DeepPyroApp can push the authoritative runtime state, and lets
the usual Select.Changed messages bubble up; the app decides whether a change is
allowed (for example, no workspace switch during a running turn) and calls
sync() again, which snaps the dropdown back when the change was rejected.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Select, Static

from deeppyro.sandbox.types import APPROVALS, PRESETS

#: Value of the last cwd option; selecting it opens the free-text path modal.
CUSTOM_WORKSPACE = "\x00custom-workspace"
CUSTOM_WORKSPACE_LABEL = "Enter path..."
#: Upper bound on remembered workspaces shown in the dropdown.
MAX_WORKSPACE_OPTIONS = 12


def _workspace_key(path: str) -> str:
    """Normalize a path for dedupe while keeping the first spelling for display."""
    try:
        return os.path.normcase(os.path.normpath(path))
    except (TypeError, ValueError):
        return path


def workspace_options(cwd: str, workspaces: Iterable[str]) -> list[tuple[str, str]]:
    """Options for the cwd dropdown: current first, then remembered, then custom."""
    ordered: list[str] = []
    seen: set[str] = set()
    for path in [str(cwd or ""), *workspaces]:
        path = str(path or "")
        if not path:
            continue
        key = _workspace_key(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    options = [(path, path) for path in ordered[:MAX_WORKSPACE_OPTIONS]]
    options.append((CUSTOM_WORKSPACE_LABEL, CUSTOM_WORKSPACE))
    return options


class ControlBar(Horizontal):
    """Three compact dropdowns above the conversation."""

    def __init__(self) -> None:
        super().__init__(id="control-bar")
        self._cwd_values: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        yield Static("policy", classes="control-label")
        yield Select(
            [(preset, preset) for preset in PRESETS],
            prompt="policy",
            allow_blank=False,
            value=PRESETS[1],
            id="select-preset",
            compact=True,
            tooltip="Sandbox permission preset for tool calls",
        )
        yield Static("approval", classes="control-label")
        yield Select(
            [(approval, approval) for approval in APPROVALS],
            prompt="approval",
            allow_blank=False,
            value=APPROVALS[0],
            id="select-approval",
            compact=True,
            tooltip="Approval policy when the preset asks for confirmation",
        )
        yield Static("cwd", classes="control-label")
        yield Select(
            [(CUSTOM_WORKSPACE_LABEL, CUSTOM_WORKSPACE)],
            prompt="cwd",
            allow_blank=True,
            id="select-cwd",
            compact=True,
            tooltip="Workspace directory; applies from the next turn",
        )

    # -- selectors (also used by tests) ---------------------------------
    @property
    def preset_select(self) -> Select[str]:
        return self.query_one("#select-preset", Select)

    @property
    def approval_select(self) -> Select[str]:
        return self.query_one("#select-approval", Select)

    @property
    def cwd_select(self) -> Select[str]:
        return self.query_one("#select-cwd", Select)

    @property
    def cwd_values(self) -> tuple[str, ...]:
        """Values currently offered by the cwd dropdown."""
        return self._cwd_values

    # -- state push ------------------------------------------------------
    def sync(
        self,
        *,
        preset: str,
        approval: str,
        cwd: str,
        workspaces: Sequence[str] = (),
        enabled: bool = True,
    ) -> None:
        """Render the authoritative runtime state and enable/disable the bar."""
        preset_select = self.preset_select
        if preset in PRESETS and preset_select.value != preset:
            preset_select.value = preset
        approval_select = self.approval_select
        if approval in APPROVALS and approval_select.value != approval:
            approval_select.value = approval
        cwd_select = self.cwd_select
        options = workspace_options(cwd, workspaces)
        values = tuple(value for _, value in options)
        if values != self._cwd_values:
            self._cwd_values = values
            cwd_select.set_options(options)
        if cwd_select.value != cwd:
            cwd_select.value = cwd
        for select in (preset_select, approval_select, cwd_select):
            if select.disabled == enabled:
                select.disabled = not enabled


__all__ = [
    "CUSTOM_WORKSPACE",
    "CUSTOM_WORKSPACE_LABEL",
    "ControlBar",
    "workspace_options",
]
