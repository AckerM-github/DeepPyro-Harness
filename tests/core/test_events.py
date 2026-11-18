"""The kernel bus catalog: what exists, how it dispatches, and who produces it."""

from __future__ import annotations

from deeppyro.core import events
from deeppyro.hooks.types import DEPRECATED_NAMES, HOOK_MODES


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


def test_the_two_tables_do_not_overlap() -> None:
    implemented = set(events.names(include_reserved=False))
    reserved = set(events.RESERVED_NAMES)
    assert set(HOOK_MODES) <= implemented
    assert implemented.isdisjoint(reserved)
    assert len(events.EVENT_SPECS) == len(implemented) + len(reserved)


def test_every_bus_name_is_a_domain_namespace() -> None:
    for name in events.names():
        assert "/" in name, name
        assert not name.startswith("internal/"), name
        assert not name.startswith("upstream/"), name


def test_modes_are_known_dispatch_verbs() -> None:
    modes = {"emit", "serial", "waterfall", "bail", "parallel"}
    for name in events.names():
        assert events.mode_of(name) in modes, name
