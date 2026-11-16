"""The bus catalog: what exists, how it dispatches, and who proves it."""

from __future__ import annotations

from deeppyro.core import events
from deeppyro.core.event_catalog import BUS_EVENTS
from deeppyro.hooks.types import DEPRECATED_NAMES, HOOK_MODES


def test_the_dsh_catalog_holds_every_bus_event() -> None:
    assert len(BUS_EVENTS) == 94
    for name, (modes, site) in BUS_EVENTS.items():
        assert "/" in name
        assert modes, name
        assert set(modes) <= {"emit", "serial", "waterfall", "bail", "parallel"}
        assert not site.startswith("dsh/node_modules/"), name


def test_every_catalog_name_is_a_known_bus_event() -> None:
    missing = sorted(set(BUS_EVENTS) - set(events.EVENT_SPECS))
    assert missing == []


def test_every_implemented_event_has_a_producer_and_a_mode() -> None:
    for name in events.names(include_reserved=False):
        spec = events.EVENT_SPECS[name]
        assert spec.producer, name
        assert spec.mode == HOOK_MODES.get(name, spec.mode)
        assert not spec.reason, name


def test_every_reserved_event_explains_itself() -> None:
    for name in sorted(events.RESERVED_NAMES):
        spec = events.EVENT_SPECS[name]
        assert spec.reason, name
        assert spec.producer, name


def test_retired_names_are_not_bus_events() -> None:
    for name in sorted(DEPRECATED_NAMES):
        assert not events.is_bus_event(name), name


def test_the_mode_conflict_list_is_the_one_deliberate_deviation() -> None:
    # `llm/stream` is the only name whose deeppyro mode is not a DSH mode: DSH
    # waterfalls the stream object, deeppyro emits a delta per chunk. The other
    # three names looked conflicting only because the catalog read subscription
    # verbs as dispatch modes; evidence now separates the two.
    assert events.mode_conflicts() == {"llm/stream": ("emit", ("waterfall",))}


def test_the_two_tables_do_not_overlap() -> None:
    implemented = set(events.names(include_reserved=False))
    assert set(HOOK_MODES) <= implemented
    assert implemented.isdisjoint(events.RESERVED_NAMES)
    assert len(events.EVENT_SPECS) == len(BUS_EVENTS) + 21


def test_no_cordis_plumbing_events_are_catalogued() -> None:
    # The native runtime raises no internal/* plumbing: a plugin can only bind a
    # domain handler to a bus name, so the catalog is domain events only.
    assert not hasattr(events, "INTERNAL_NAMES")
    assert not hasattr(events, "internal_spec")
    assert [name for name in events.names() if name.startswith("internal/")] == []
    assert [name for name in events.EVENT_SPECS if name.startswith("internal/")] == []


# The P1 namespaces named by the alignment brief. They are declared, not built:
# the bus must know their names so a plugin can subscribe before the subsystem
# exists, but nothing dispatches them until a real producer lands.
P1_NAMESPACES = {
    "agent-preset",
    "api-session",
    "app-boot",
    "command",
    "compaction",
    "credentials",
    "fs",
    "goal",
    "jobs",
    "loader",
    "mcp",
    "sandbox",
    "schedule",
    "settings",
    "subagent",
    "team",
    "web",
    "workspace",
}


def test_every_p1_namespace_is_declared_with_evidence() -> None:
    declared = {name.split("/", 1)[0] for name in events.EVENT_SPECS}
    missing = sorted(P1_NAMESPACES - declared)
    assert missing == [], f"P1 namespaces missing from the bus table: {missing}"

    for name in events.EVENT_SPECS:
        if name.split("/", 1)[0] not in P1_NAMESPACES:
            continue
        spec = events.EVENT_SPECS[name]
        if spec.implemented:
            assert spec.producer, name
        else:
            assert spec.reason, f"{name} is reserved without a reason"
            assert spec.dsh_ref or spec.reason, name
