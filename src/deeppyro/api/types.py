"""Plain data types shared by plugins and the host.

Nothing here imports the kernel or the vendored Cordis runtime, so an external
plugin only needs the installed deeppyro package for these declarations.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    section is visible to every agent.
    """

    name: str
    order: int
    renderer: Callable[[Any, Any], str]
    plugin_id: str = ""
    scope: str | None = None


@dataclass(frozen=True, slots=True)
class PresetSpec:
    """One named agent composition selected per session.

    A preset is a scope label rather than a container: tools and prompt
    sections registered with scope=<preset id> are only visible to agents
    created with that preset, on top of the global contributions.
    """

    id: str
    name: str = ""
    description: str = ""
    model: str = ""
    instructions: tuple[str, ...] = ()
    order: int = 100
    plugin_id: str = ""

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


__all__ = [
    "CommandSpec",
    "PluginStatus",
    "PresetSpec",
    "PromptSection",
    "ProviderContribution",
    "ToolContribution",
]
