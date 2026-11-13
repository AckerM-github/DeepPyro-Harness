"""Plain data types shared by plugins and the host.

Nothing here imports the kernel or the vendored Cordis runtime, so an external
plugin only needs the installed deeppyro package for these declarations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """One slash command contributed by a plugin.

    The handler receives the host application object and the raw argument
    string. It may be a plain function or an async function; the command
    service awaits awaitable results.
    """

    name: str
    summary: str = ""
    usage: str = ""
    aliases: tuple[str, ...] = ()
    handler: Callable[[Any, str], Any | Awaitable[Any]] | None = None
    plugin_id: str = ""

    def matches(self, token: str) -> bool:
        lowered = token.lower()
        return lowered == self.name or lowered in (alias.lower() for alias in self.aliases)


@dataclass(frozen=True, slots=True)
class PromptSection:
    """A named, ordered chunk of the system prompt.

    A non-None scope reserves the section for one agent preset; None means the
    section is visible to every agent. A non-None revision narrows a scoped
    section to one immutable preset revision: sections contributed by a preset
    realm plugin are tagged with the revision that was materialized.
    """

    name: str
    order: int
    renderer: Callable[[Any, Any], str]
    plugin_id: str = ""
    scope: str | None = None
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class PresetPluginRow:
    """One plugin row carried by a preset revision.

    Rows are mounted inside the preset realm, a child context of the host
    composition, when the revision is first materialized. The plugin target
    uses the same syntax as a bundle row: 'module:Class' or a '.py' path
    relative to :attr:`PresetSpec.base`.
    """

    id: str
    plugin: str
    config: dict[str, Any] | None = None
    disabled: bool = False
    #: Service names to isolate inside the realm before mounting this row.
    isolate: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PresetSpec:
    """One named agent composition selected per session.

    A preset is both a scope label and an optional container: tools and prompt
    sections registered with scope=<preset id> are visible to agents created
    with that preset, and :attr:`plugins` rows are mounted once per revision in
    a child context. Every registration is a new immutable revision; live
    agents keep the revision they were created with.
    """

    id: str
    name: str = ""
    description: str = ""
    model: str = ""
    instructions: tuple[str, ...] = ()
    order: int = 100
    plugin_id: str = ""
    #: Plugin rows mounted inside the preset realm (DSH per-preset plugins).
    plugins: tuple[PresetPluginRow, ...] = ()
    #: Host-filled: the immutable revision of this preset snapshot.
    revision: int = 0
    #: Agent mode selected by this preset. default exposes single-agent tools
    #: (including delegation); team swaps in the agent-team toolset.
    mode: str = "default"
    #: Base directory used to resolve relative '.py' plugin targets.
    base: Path | None = None

    def display_name(self) -> str:
        return self.name or self.id


@dataclass(frozen=True, slots=True)
class ToolContribution:
    """A tool factory contributed by a plugin.

    The factory is called with (config, workspace) and must return a tool
    object. Scope is None for host-wide tools, or an agent-preset id for
    scoped tools.
    """

    factory: Callable[[Any, Any], Any]
    scope: str | None = None
    plugin_id: str = ""
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderContribution:
    """A named LLM provider factory contributed by a plugin."""

    name: str
    factory: Callable[[Any], Any]
    plugin_id: str = ""
    description: str = ""


@dataclass(slots=True)
class PluginStatus:
    """Runtime status of one plugin row."""

    id: str
    plugin: str
    state: str = "pending"
    critical: bool = False
    origin: str = ""
    error: str = ""
    effects: list[str] = field(default_factory=list)


#: Canonical preset ids understood by the host.
DEFAULT_PRESET_ID = "default"
TEAM_PRESET_ID = "team"

#: Agent modes carried by PresetSpec.mode.
AGENT_MODE_DEFAULT = "default"
AGENT_MODE_TEAM = "team"

#: Tools contributed by the built-in team plugin.
TEAM_TOOL_NAMES: tuple[str, ...] = (
    "team_spawn",
    "team_send",
    "team_wait",
    "team_cancel",
    "team_members",
    "team_task_create",
    "team_task_list",
    "team_task_get",
    "team_task_update",
)

#: Tools contributed by the built-in subagent plugin.
SUBAGENT_TOOL_NAMES: tuple[str, ...] = ("subagent",)

#: Delegation tools hidden from the lead agent while in team mode.
TEAM_MODE_DISABLED_TOOLS: tuple[str, ...] = SUBAGENT_TOOL_NAMES

#: Tools hidden from spawned children so they cannot nest orchestration.
CHILD_DISABLED_TOOLS: tuple[str, ...] = TEAM_TOOL_NAMES + SUBAGENT_TOOL_NAMES

__all__ = [
    "AGENT_MODE_DEFAULT",
    "AGENT_MODE_TEAM",
    "CHILD_DISABLED_TOOLS",
    "DEFAULT_PRESET_ID",
    "SUBAGENT_TOOL_NAMES",
    "TEAM_MODE_DISABLED_TOOLS",
    "TEAM_PRESET_ID",
    "TEAM_TOOL_NAMES",
    "CommandSpec",
    "PluginStatus",
    "PresetPluginRow",
    "PresetSpec",
    "PromptSection",
    "ProviderContribution",
    "ToolContribution",
]
