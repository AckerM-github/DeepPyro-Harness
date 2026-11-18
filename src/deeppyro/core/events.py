"""The kernel event catalog: one bus, one list of domain events.

Every domain event goes through one bus. A name either exists (with a mode and
a payload) or it does not exist at all; there is no second, private channel.
The transport lives in :class:`deeppyro.hooks.service.HookService` (also exposed
as service ``hooks``), and this module answers three questions for every name:
does it exist, how does it dispatch, and who produces it.

The catalog contains only names deeppyro itself dispatches, plus a small set of
reserved domain names that a plugin may subscribe to before the producing
subsystem exists. Reserved names carry a reason and have no producer yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from deeppyro.hooks.types import HOOK_MODES

__all__ = [
    "EVENT_SPECS",
    "RESERVED_NAMES",
    "EventMode",
    "EventSpec",
    "EventStatus",
    "is_bus_event",
    "mode_of",
    "names",
    "producer_of",
    "spec",
]

EventMode = Literal["emit", "serial", "waterfall", "bail", "parallel"]
EventStatus = Literal["implemented", "reserved"]


@dataclass(frozen=True, slots=True)
class EventSpec:
    """One bus event: its dispatch mode, who dispatches it, and whether it exists."""

    name: str
    mode: EventMode
    status: EventStatus = "implemented"
    producer: str = ""
    #: why a reserved name exists without a producer.
    reason: str = ""

    @property
    def implemented(self) -> bool:
        return self.status == "implemented"


#: producer of every name deeppyro dispatches today.
_PRODUCERS: dict[str, str] = {
    "agent/pre-step": "agent.loop",
    "agent/request": "agent.loop",
    "agent/request-error": "agent.loop",
    "agent/turn-stopping": "agent.loop",
    "agent/assistant-stream": "agent.loop",
    "agent/status": "agent.loop",
    "system-prompt/assemble": "prompt.service",
    "llm/stream": "agent.loop",
    "session/created": "sessions.service",
    "session/disposed": "sessions.service",
    "agent/error": "agent.loop",
    "tools/pre-execute": "tools.pipeline",
    "tools/execute": "tools.pipeline",
    "tools/post-execute": "tools.pipeline",
    "approval/request": "sandbox.approval",
    "command/executed": "commands.service",
    "workspace/changed": "working_directory.service",
    "goal/changed": "goal.service",
    "mcp/connected": "mcp.service",
    "mcp/disconnected": "mcp.service",
    "agent/inbox/inserted": "agent.inbox",
    "agent/inbox/claimed": "agent.inbox",
    "agent/inbox/discarded": "agent.inbox",
    "goal/activation-changed": "goal.service",
}

#: Implemented events whose mode is not declared by the legacy hook surface.
_EXTRA_MODES: dict[str, EventMode] = {
    "command/executed": "emit",
    "workspace/changed": "emit",
    "goal/changed": "emit",
    "goal/activation-changed": "emit",
    "mcp/connected": "emit",
    "mcp/disconnected": "emit",
    "agent/inbox/inserted": "emit",
    "agent/inbox/claimed": "emit",
    "agent/inbox/discarded": "emit",
}

#: Reserved domain names a plugin may subscribe to before a producer exists.
_RESERVED: dict[str, str] = {
    "api-session/activity": "api-session",
    "api-session/added": "api-session",
    "api-session/removed": "api-session",
    "api-session/updated": "api-session",
    "compaction/prune": "compaction",
    "compaction/start": "compaction",
    "compaction/end": "compaction",
    "credentials/updated": "credentials",
    "fs/changed": "fs",
    "jobs/updated": "jobs",
    "loader/changed": "loader",
    "llm/selection": "llm",
    "plugin/status": "plugin",
    "sandbox/policy": "sandbox",
    "schedule/fired": "schedule",
    "settings/updated": "settings",
    "subagent/completed": "subagent",
    "subagent/started": "subagent",
    "team/member-added": "team",
    "team/member-removed": "team",
    "tools/result": "tools",
    "web/fetched": "web",
    "workspace/changed": "workspace",
}


def _build_specs() -> dict[str, EventSpec]:
    """Merge implemented names with the reserved domain-name table."""

    specs: dict[str, EventSpec] = {}
    for name in HOOK_MODES:
        if name not in _PRODUCERS:
            raise RuntimeError(f"bus event {name!r} has a mode but no producer")
    for name, producer in _PRODUCERS.items():
        mode = HOOK_MODES.get(name) or _EXTRA_MODES.get(name)
        if mode is None:
            raise RuntimeError(f"bus event {name!r} has no mode")
        specs[name] = EventSpec(
            name=name,
            mode=mode,  # type: ignore[arg-type]
            status="implemented",
            producer=producer,
        )
    for name, namespace in _RESERVED.items():
        if name in specs:
            continue
        specs[name] = EventSpec(
            name=name,
            mode="emit",
            status="reserved",
            producer=namespace,
            reason="reserved domain event; no producer dispatches it yet",
        )
    return specs


EVENT_SPECS: dict[str, EventSpec] = _build_specs()

RESERVED_NAMES: frozenset[str] = frozenset(
    name for name, event in EVENT_SPECS.items() if event.status == "reserved"
)


def spec(name: str) -> EventSpec | None:
    """Return the catalog entry for ``name`` (``None`` when it is not a bus event)."""
    return EVENT_SPECS.get(name)


def is_bus_event(name: str) -> bool:
    """Whether ``name`` is a domain event on the kernel bus."""
    return name in EVENT_SPECS


def mode_of(name: str) -> EventMode | None:
    """Dispatch mode of ``name``, when the catalog knows it."""
    event = EVENT_SPECS.get(name)
    return None if event is None else event.mode


def producer_of(name: str) -> str:
    """Who dispatches ``name`` ('' for reserved or unknown names)."""
    event = EVENT_SPECS.get(name)
    return "" if event is None else event.producer


def names(*, include_reserved: bool = True) -> tuple[str, ...]:
    """Every known bus name, sorted; reserved names are opt-out."""
    return tuple(
        sorted(
            name for name, event in EVENT_SPECS.items() if include_reserved or event.status == "implemented"
        )
    )
