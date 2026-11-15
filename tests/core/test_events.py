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
        assert spec.mode == HOOK_MODES[name]
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
    assert implemented == set(HOOK_MODES)
    assert implemented.isdisjoint(events.RESERVED_NAMES)
    assert len(events.EVENT_SPECS) == len(BUS_EVENTS) + 21
