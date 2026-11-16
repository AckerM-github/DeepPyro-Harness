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

from deeppyro.core.event_catalog import BUS_EVENTS
from deeppyro.hooks.types import HOOK_MODES

__all__ = [
    "EVENT_SPECS",
    "INTERNAL_NAMES",
    "INTERNAL_SPECS",
    "RESERVED_NAMES",
    "EventMode",
    "EventSpec",
    "EventStatus",
    "internal_spec",
    "is_bus_event",
    "mode_conflicts",
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
    #: modes DSH declares or uses for this name, when the M0 catalog knows it.
    dsh_modes: tuple[str, ...] = ()
    #: why a reserved name exists without a producer.
    reason: str = ""

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
    "command/executed": "commands.service",
    "workspace/changed": "working_directory.service",
    "goal/changed": "goal.service",
    "mcp/connected": "mcp.service",
    "mcp/disconnected": "mcp.service",
    "agent/inbox/inserted": "agent.session",
    "agent/inbox/claimed": "agent.session",
    "agent/inbox/discarded": "agent.session",
    "goal/activation-changed": "goal.service",
}

#: Implemented events that are not in the DSH catalogue yet: their mode is
#: declared here instead of being inferred from a call site.
_EXTRA_MODES: dict[str, str] = {
    "workspace/changed": "emit",
    "mcp/connected": "emit",
    "mcp/disconnected": "emit",
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


#: The ten cordis plumbing events the vendored port already raises. They are not
#: domain events: they fire inside the port's own hook table (fiber.py, reflect.py,
#: events.py), carry cordis objects rather than domain payloads, and deliberately
#: stay out of ``EVENT_SPECS`` so ``ctx.on`` cannot bind a plugin to them.
_INTERNAL: dict[str, tuple[str, str, str]] = {
    "internal/commit": ("emit", "cordis.fiber", "'internal/commit'(fiber: Fiber, originalConfig: any): void"),
    "internal/config": (
        "waterfall",
        "cordis.events",
        "'internal/config'(fiber: Fiber, next: () => any): any",
    ),
    "internal/dispatch": (
        "emit",
        "cordis.events",
        "'internal/dispatch'(mode: DispatchMode, name: string | symbol, args: any[], thisArg: any): void",
    ),
    "internal/get": (
        "waterfall",
        "cordis.reflect",
        "'internal/get'(ctx: Context, name: string, error: Error, next: () => any): any",
    ),
    "internal/listener": (
        "bail",
        "cordis.events",
        "'internal/listener'(this: Context, name: string, listener: any, prepend: boolean): void",
    ),
    "internal/plugin": ("emit", "cordis.fiber", "'internal/plugin'(fiber: Fiber): void"),
    "internal/service": (
        "emit",
        "cordis.reflect",
        "'internal/service'(this: Context, name: string, value: any): void",
    ),
    "internal/set": (
        "waterfall",
        "cordis.reflect",
        "'internal/set'(ctx: Context, name: string, value: any, error: Error, next: () => boolean): boolean",
    ),
    "internal/status": (
        "emit",
        "cordis.fiber",
        "'internal/status'(fiber: Fiber, oldValue: FiberState): void",
    ),
    "internal/update": (
        "waterfall",
        "cordis.events",
        "'internal/update'(this: Fiber, resolvedConfig: any, next: () => Awaitable<void>): Awaitable<void>",
    ),
}

INTERNAL_SPECS: dict[str, EventSpec] = {
    name: EventSpec(
        name=name,
        mode=mode,  # type: ignore[arg-type]
        status="implemented",
        producer=producer,
        dsh_ref=signature,
        dsh_modes=(mode,),
        reason="cordis plumbing; not a domain event",
    )
    for name, (mode, producer, signature) in _INTERNAL.items()
}

INTERNAL_NAMES: frozenset[str] = frozenset(INTERNAL_SPECS)


def internal_spec(name: str) -> EventSpec | None:
    """Return the plumbing spec for an ``internal/*`` event (``None`` otherwise)."""
    return INTERNAL_SPECS.get(name)


def _catalog_entry(name: str) -> tuple[tuple[str, ...], str]:
    return BUS_EVENTS.get(name, ((), ""))


def _build_specs() -> dict[str, EventSpec]:
    """Merge the three sources: what deeppyro dispatches, the DSH catalog, extras."""

    specs: dict[str, EventSpec] = {}
    for name in HOOK_MODES:
        if name not in _PRODUCERS:
            raise RuntimeError(f"bus event {name!r} has a mode but no producer")
    # Producers that are not legacy hook names take their mode from the DSH
    # catalog: the name policy in hooks/types.py only covers the old surface.
    for name in _PRODUCERS.keys() - HOOK_MODES.keys():
        modes, _site = _catalog_entry(name)
        if not modes and name not in _EXTRA_MODES:
            raise RuntimeError(f"bus event {name!r} has no mode: add it to the catalog")
    for name, mode in HOOK_MODES.items():
        modes, site = _catalog_entry(name)
        specs[name] = EventSpec(
            name=name,
            mode=mode,  # type: ignore[arg-type]
            status="implemented",
            producer=_PRODUCERS[name],
            dsh_ref=site,
            dsh_modes=modes,
        )
    for name in _PRODUCERS.keys() - HOOK_MODES.keys():
        if name in specs:
            continue
        modes, site = _catalog_entry(name)
        specs[name] = EventSpec(
            name=name,
            mode=(_EXTRA_MODES.get(name) or modes[0]),  # type: ignore[arg-type]
            status="implemented",
            producer=_PRODUCERS[name],
            dsh_ref=site,
            dsh_modes=modes,
        )
    for name, (modes, site) in BUS_EVENTS.items():
        if name in specs:
            continue
        specs[name] = EventSpec(
            name=name,
            mode=modes[0],  # type: ignore[arg-type]
            status="reserved",
            producer=f"dsh/{name.split('/', 1)[0]}",
            dsh_ref=site,
            dsh_modes=modes,
            reason="DSH bus event with no deeppyro producer yet",
        )
    for name, namespace in _RESERVED.items():
        if name in specs:
            continue
        specs[name] = EventSpec(
            name=name,
            mode="emit",
            status="reserved",
            producer=f"dsh/{namespace}",
            dsh_ref=f"dsh/{namespace}",
            reason="declared by deeppyro; no counterpart in the M0 DSH catalog",
        )
    return specs


EVENT_SPECS: dict[str, EventSpec] = _build_specs()

RESERVED_NAMES: frozenset[str] = frozenset(
    name for name, event in EVENT_SPECS.items() if event.status == "reserved"
)


def mode_conflicts() -> dict[str, tuple[str, tuple[str, ...]]]:
    """Implemented names whose deeppyro mode is not one DSH uses (M3 work list)."""

    return {
        name: (event.mode, event.dsh_modes)
        for name, event in sorted(EVENT_SPECS.items())
        if event.implemented and event.dsh_modes and event.mode not in event.dsh_modes
    }


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
