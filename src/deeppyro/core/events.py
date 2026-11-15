"""The kernel event catalog: one bus, one list of domain events.

DSH dispatches every domain event through a single cordis ``EventsService``. A
name either exists there (with a mode and a payload) or it does not exist at
all; there is no second, private channel. deeppyro keeps that shape: the
transport lives in :class:`deeppyro.hooks.service.HookService` (also exposed as
service ``hooks``), and this module is the catalog that answers three questions
for every name -- does it exist, how does it dispatch, and who produces it.

The M0 catalog (``docs/spec/dsh-event-catalog.md``) holds the full DSH list of
94 bus events. Only the ones deeppyro actually dispatches are ``implemented``;
the rest are ``reserved``: they are recognised so a plugin gets a clear error
instead of a silent no-op, but nothing emits them yet. Reserved names carry the
DSH namespace as their producer so the gap stays visible.
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
    dsh_ref: str = ""

    @property
    def implemented(self) -> bool:
        return self.status == "implemented"


#: producer of every name deeppyro dispatches today.
_PRODUCERS: dict[str, str] = {
    "agent/pre-step": "agent.session",
    "agent/request": "agent.session",
    "agent/request-error": "agent.session",
    "agent/turn-stopping": "agent.session",
    "agent/assistant-stream": "agent.session",
    "agent/status": "agent.session",
    "system-prompt/assemble": "prompt.service",
    "llm/stream": "agent.session",
    "session/created": "session.store",
    "session/disposed": "session.store",
    "agent/error": "agent.session",
    "tools/pre-execute": "tools.pipeline",
    "tools/execute": "tools.pipeline",
    "tools/post-execute": "tools.pipeline",
    "approval/request": "sandbox.approval",
}

#: DSH bus events deeppyro recognises but does not dispatch yet. Kept explicit
#: (not generated) so a reviewer can see the intent and the DSH namespace.
_RESERVED: dict[str, str] = {
    "agent/request-error": "agent",
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
    "mcp/connected": "mcp",
    "mcp/disconnected": "mcp",
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

EVENT_SPECS: dict[str, EventSpec] = {
    name: EventSpec(
        name=name,
        mode=mode,  # type: ignore[arg-type]
        status="implemented",
        producer=_PRODUCERS.get(name, ""),
        dsh_ref=f"dsh/{name.split('/', 1)[0]}",
    )
    for name, mode in HOOK_MODES.items()
}

for _name, _namespace in _RESERVED.items():
    EVENT_SPECS.setdefault(
        _name,
        EventSpec(
            name=_name,
            mode="emit",
            status="reserved",
            producer=f"dsh/{_namespace}",
            dsh_ref=f"dsh/{_namespace}",
        ),
    )

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
    """Who dispatches ``name`` today ('' for reserved or unknown names)."""
    event = EVENT_SPECS.get(name)
    return "" if event is None else event.producer


def names(*, include_reserved: bool = True) -> tuple[str, ...]:
    """Every known bus name, sorted; reserved names are opt-out."""
    return tuple(
        sorted(
            name for name, event in EVENT_SPECS.items() if include_reserved or event.status == "implemented"
        )
    )
