"""Parity-test adapter for the builtin activity projections.

The four units (todos, jobs, agents, plan_mode) now live in production code at
`deeppyro.builtin.projections`; this module only re-exports them so the parity
tests exercise the real definitions instead of a copy.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from deeppyro.builtin.projections import (
    PROJECTION_KEYS,
    definitions,
    register_all,
)
from deeppyro.core.wire import SessionHeader
from deeppyro.projection import ProjectionDefinition, ProjectionHandle, ProjectionRegistry


def wire_values(registry: ProjectionRegistry, session: Any) -> dict[str, Any]:
    """Snapshot of all four activity units for session."""
    return registry.snapshot(session, keys=list(PROJECTION_KEYS))


def header_of(session: Any) -> SessionHeader:
    """Small typed accessor used by tests to assert init arguments."""
    return session.header


def iter_definitions() -> Iterable[ProjectionDefinition]:
    """Iterate the activity definitions (helper for parametrised tests)."""
    return iter(definitions())


__all__ = [
    "PROJECTION_KEYS",
    "ProjectionHandle",
    "definitions",
    "header_of",
    "iter_definitions",
    "register_all",
    "wire_values",
]
