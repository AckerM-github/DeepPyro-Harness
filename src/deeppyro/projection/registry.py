"""In-process session projection registry.

A projection is a pure fold over the durable session log.  init seeds a cell
from the session header and the fork-inherited event count, apply advances that
cell by one LogRecord, and an optional wire callable derives the client-visible
value.  The registry owns one cell per (session_id, key), advances cells
incrementally, and notifies change listeners once per changed client-visible
unit.

Design rules (mirrored from DSH session projection):

* apply is synchronous and total: an unrelated record MUST return the same
  state object (identity gate), a changed record MUST return the complete next
  state (never a bare delta).
* Cells are lazy: a unit registered after events flowed, or a session opened
  after registration, folds the full log on first touch.
* The registry never writes the session log.  Callers that append a record call
  ProjectionRegistry.notify explicitly (this port does not hook
  Session.append), so a missed notification self-heals on the next one.
* Reads (snapshot, state_of) materialize cells to the session cursor but never
  notify; only notify drives the change feed.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord, SessionHeader

if TYPE_CHECKING:
    from deeppyro.session.session import Session

logger = logging.getLogger("deeppyro.projection")

# Sentinel meaning that no client view has been computed for a cell yet.
_NO_VIEW = object()


class ProjectionError(SessionError, ValueError):
    """Projection registration, restore, or checkpoint contract violation.

    Also a ValueError so a host validating a plugin definition can treat it as
    a bad argument.
    """


@dataclass(frozen=True, slots=True)
class ProjectionDefinition:
    """One pure fold that the registry drives over committed session events.

    Attributes:
        key: Stable projection name.  Re-registering the same key with the same
            state_version and summary flag shares one unit (and its cells).
        state_version: Bumped whenever apply changes shape or semantics.
            Durable checkpoint rows carrying any other version are discarded.
        init: (header, inherited_event_count) -> initial state.
        apply: (state, record) -> next state.  MUST return state itself for
            records it does not care about (identity gate).
        wire: Optional state -> value producing the client-visible value used
            for change notifications and snapshots.
        summary: Whether the unit belongs to session summaries.  This module
            only stores the flag; the projections plugin interprets it.
    """

    key: str
    state_version: int
    init: Callable[[SessionHeader, int], Any]
    apply: Callable[[Any, LogRecord], Any]
    wire: Callable[[Any], Any] | None = None
    summary: bool = True


class _Cell:
    """Materialized state of one unit for one session."""

    __slots__ = ("last_view", "observed_seq", "state")

    def __init__(self, state: Any, observed_seq: int = -1) -> None:
        self.state = state
        self.observed_seq = observed_seq
        self.last_view: Any = _NO_VIEW


class _Registration:
    """One live unit: its definition, refcount and per-session cells."""

    __slots__ = ("cells", "definition", "refs")

    def __init__(self, definition: ProjectionDefinition) -> None:
        self.definition = definition
        self.refs = 1
        self.cells: dict[str, _Cell] = {}


class ProjectionHandle:
    """Read handle returned by ProjectionRegistry.register.

    The handle stays usable after dispose: a retained reader still folds events
    on demand (DSH parity); it only stops keeping the unit in the live
    notification and snapshot set.
    """

    __slots__ = ("_disposed", "_registration", "_registry")

    def __init__(self, registry: ProjectionRegistry, registration: _Registration) -> None:
        self._registry = registry
        self._registration = registration
        self._disposed = False

    @property
    def key(self) -> str:
        """Projection key this handle refers to."""
        return self._registration.definition.key

    @property
    def state_version(self) -> int:
        """State version of the shared unit this handle refers to."""
        return self._registration.definition.state_version

    @property
    def disposed(self) -> bool:
        """Whether this handle already released its reference."""
        return self._disposed

    def state_of(self, session: Session) -> Any:
        """Current state for session, folding the log lazily when needed."""
        return self._registry._state_of(self._registration, session)

    def reader_for(self, session: Session) -> Callable[[], Any]:
        """Return a zero-argument reader bound to session."""
        handle = self

        def read() -> Any:
            return handle.state_of(session)

        return read

    def dispose(self) -> None:
        """Release this handle (idempotent per handle)."""
        if self._disposed:
            return
        self._disposed = True
        self._registry._release(self._registration)

    def __repr__(self) -> str:
        return (
            f"ProjectionHandle(key={self.key!r}, state_version={self.state_version}, "
            f"disposed={self._disposed})"
        )


class ProjectionRegistry:
    """Registry of projection units with per-session incremental cells."""

    def __init__(self) -> None:
        self._registrations: dict[str, _Registration] = {}
        self._listeners: list[Callable[[str, str, Any], None]] = []

    # -- registration ------------------------------------------------------

    def register(self, definition: ProjectionDefinition) -> ProjectionHandle:
        """Register definition and return a read handle.

        Re-registering an existing key with an identical state_version and
        summary flag shares the unit and increments its refcount.

        Raises:
            ProjectionError: the definition is malformed, or the key is already
                registered with a different state_version or summary flag.
        """
        _validate_definition(definition)
        existing = self._registrations.get(definition.key)
        if existing is None:
            registration = _Registration(definition)
            self._registrations[definition.key] = registration
        else:
            current = existing.definition
            if current.state_version != definition.state_version:
                raise ProjectionError(
                    f"projection key {definition.key!r} is already registered at "
                    f"state_version {current.state_version}; refusing to share it with "
                    f"state_version {definition.state_version}"
                )
            if current.summary != definition.summary:
                raise ProjectionError(
                    f"projection key {definition.key!r} is already registered with "
                    f"summary={current.summary}; refusing to share it with "
                    f"summary={definition.summary}"
                )
            existing.refs += 1
            registration = existing
        return ProjectionHandle(self, registration)

    def keys(self) -> tuple[str, ...]:
        """Live projection keys, in registration order."""
        return tuple(key for key, reg in self._registrations.items() if reg.refs > 0)

    def _release(self, registration: _Registration) -> None:
        if registration.refs <= 0:
            return
        registration.refs -= 1
        if registration.refs == 0:
            # Cells are retained: a released unit still reads and checkpoints
            # its last state until it registers again (DSH parity).
            logger.debug(
                "projection %s released; its cells are retained",
                registration.definition.key,
            )

    # -- reads -------------------------------------------------------------

    def state_of(self, session: Session, key: str) -> Any:
        """Current state of live projection key for session (convenience)."""
        registration = self._registrations.get(key)
        if registration is None or registration.refs == 0:
            raise ProjectionError(f"projection key {key!r} is not registered")
        return self._state_of(registration, session)

    def snapshot(self, session: Session, *, keys: Iterable[str] | None = None) -> dict[str, Any]:
        """Client-visible values for every live unit that has a wire view.

        The snapshot is one consistent cut at session.last_seq; keys without a
        wire callable are omitted.  as_of_seq returns the matching cursor (the
        values half of DSH's snapshot shape).
        """
        selected = None if keys is None else set(keys)
        values: dict[str, Any] = {}
        for registration in self._registrations.values():
            definition = registration.definition
            if registration.refs == 0 or definition.wire is None:
                continue
            if selected is not None and definition.key not in selected:
                continue
            values[definition.key] = definition.wire(self._state_of(registration, session))
        return values

    def as_of_seq(self, session: Session) -> int:
        """Last event seq covered by snapshot() for session."""
        self._materialize(session)
        return session.last_seq

    # -- change feed -------------------------------------------------------

    def on_changed(self, listener: Callable[[str, str, Any], None]) -> Callable[[], None]:
        """Subscribe to client-visible projection changes.

        The listener is called as listener(session_id, key, value) only when a
        live unit with a wire view produces a value that is not identical to the
        previously notified one.  Listener exceptions are logged and swallowed
        so one bad subscriber cannot break the turn loop.

        Returns:
            An idempotent unsubscribe callable.
        """
        if not callable(listener):
            raise ProjectionError("projection change listener must be callable")
        self._listeners.append(listener)
        listeners = self._listeners

        def unsubscribe() -> None:
            for index, current in enumerate(listeners):
                if current is listener:
                    del listeners[index]
                    return

        return unsubscribe

    def notify(self, session: Session, record: LogRecord) -> None:
        """Advance every live unit by record (called after appending it).

        A missed notification self-heals: the next notify folds every record
        observed_seq has not seen yet, in log order.
        """
        if not isinstance(record, LogRecord):
            raise ProjectionError("notify() expects a LogRecord")
        changes: list[tuple[str, Any]] = []
        for registration in list(self._registrations.values()):
            if registration.refs == 0:
                continue
            definition = registration.definition
            cell = registration.cells.get(session.session_id)
            if cell is None:
                # Fresh cell: replay the log strictly before the notified
                # record so the record itself is applied exactly once.
                cell = self._build_cell(registration, session, below=record.seq)
                registration.cells[session.session_id] = cell
            elif cell.observed_seq < record.seq:
                self._advance(registration, cell, session, below=record.seq)
            if cell.observed_seq >= record.seq:
                continue
            previous = cell.state
            cell.state = definition.apply(previous, record)
            cell.observed_seq = record.seq
            if cell.state is previous or definition.wire is None:
                continue
            view = definition.wire(cell.state)
            if view is cell.last_view:
                continue
            cell.last_view = view
            changes.append((definition.key, view))
        for key, value in changes:
            for listener in list(self._listeners):
                try:
                    listener(session.session_id, key, value)
                except Exception:
                    logger.exception("projection change listener failed for key %s", key)

    # -- checkpoints -------------------------------------------------------

    def checkpoint(self, session: Session) -> dict[str, dict[str, Any]]:
        """Durable rows for every known unit: {key: {ver, seq, val}}.

        Live units are materialized to session.last_seq first; a released unit
        still writes its last known cell, matching DSH.
        """
        rows: dict[str, dict[str, Any]] = {}
        for registration in self._registrations.values():
            definition = registration.definition
            if registration.refs > 0:
                cell = self._cell_for(registration, session)
            else:
                cell = registration.cells.get(session.session_id)
                if cell is None:
                    continue
            rows[definition.key] = {
                "ver": definition.state_version,
                "seq": cell.observed_seq,
                "val": copy.deepcopy(cell.state),
            }
        return rows

    def restore(self, session: Session, checkpoint: Mapping[str, Any]) -> None:
        """Install cells from checkpoint and replay the log tail after them.

        A row is usable only when its state_version matches, its seq is within
        the log ([−1, last_seq]), and it carries a value; otherwise the unit is
        rebuilt from a full replay, which is always correct.  Unknown keys are
        ignored; no listener is notified by a restore.
        """
        if not isinstance(checkpoint, Mapping):
            raise ProjectionError("checkpoint must be a mapping of key -> row")
        for registration in self._registrations.values():
            if registration.refs == 0:
                continue
            definition = registration.definition
            row = checkpoint.get(definition.key)
            if _row_usable(row, definition.state_version, session.last_seq):
                cell = _Cell(copy.deepcopy(row["val"]), int(row["seq"]))
                registration.cells[session.session_id] = cell
                self._advance(registration, cell, session, through=session.last_seq)
            else:
                registration.cells.pop(session.session_id, None)
                self._build_cell(registration, session)

    # -- internals ---------------------------------------------------------

    def _state_of(self, registration: _Registration, session: Session) -> Any:
        return self._cell_for(registration, session).state

    def _cell_for(self, registration: _Registration, session: Session) -> _Cell:
        cell = registration.cells.get(session.session_id)
        if cell is None:
            cell = self._build_cell(registration, session)
            registration.cells[session.session_id] = cell
        elif cell.observed_seq < session.last_seq:
            self._advance(registration, cell, session, through=session.last_seq)
        return cell

    def _build_cell(
        self,
        registration: _Registration,
        session: Session,
        *,
        below: int | None = None,
    ) -> _Cell:
        definition = registration.definition
        state = definition.init(session.header, inherited_event_count(session))
        cell = _Cell(state)
        for record in session.records:
            if below is not None and record.seq >= below:
                break
            cell.state = definition.apply(cell.state, record)
            cell.observed_seq = record.seq
        return cell

    def _advance(
        self,
        registration: _Registration,
        cell: _Cell,
        session: Session,
        *,
        through: int | None = None,
        below: int | None = None,
    ) -> None:
        definition = registration.definition
        for record in session.records:
            seq = record.seq
            if seq <= cell.observed_seq:
                continue
            if through is not None and seq > through:
                break
            if below is not None and seq >= below:
                break
            cell.state = definition.apply(cell.state, record)
            cell.observed_seq = seq

    def _materialize(self, session: Session) -> None:
        for registration in self._registrations.values():
            if registration.refs > 0:
                self._cell_for(registration, session)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_definition(definition: ProjectionDefinition) -> None:
    if not isinstance(definition, ProjectionDefinition):
        raise ProjectionError("register() expects a ProjectionDefinition")
    if not isinstance(definition.key, str) or not definition.key.strip():
        raise ProjectionError("projection key must be a non-empty string")
    if not _is_int(definition.state_version) or definition.state_version < 0:
        raise ProjectionError(f"projection {definition.key!r} state_version must be a non-negative int")
    if not callable(definition.init):
        raise ProjectionError(f"projection {definition.key!r} init must be callable")
    if not callable(definition.apply):
        raise ProjectionError(f"projection {definition.key!r} apply must be callable")
    if definition.wire is not None and not callable(definition.wire):
        raise ProjectionError(f"projection {definition.key!r} wire must be callable or None")
    if not isinstance(definition.summary, bool):
        raise ProjectionError(f"projection {definition.key!r} summary must be a bool")


def _row_usable(row: Any, state_version: int, last_seq: int) -> bool:
    if not isinstance(row, Mapping):
        return False
    version = row.get("ver")
    seq = row.get("seq")
    if not _is_int(version) or version != state_version:
        return False
    if not _is_int(seq) or seq < -1 or seq > last_seq:
        return False
    return "val" in row


def inherited_event_count(session: Session) -> int:
    """Number of log events inherited from a parent session at a fork cut.

    P0 fork inserts a session/end-seed record with data {inherited: true} at the
    cut, so the count is that record's position in the event list (positions,
    not seq arithmetic, so trimmed logs stay correct).  Sessions without the
    marker inherit nothing.
    """
    for index, record in enumerate(session.records):
        if record.type != "session/end-seed":
            continue
        data = record.data
        if isinstance(data, Mapping) and data.get("inherited") is True:
            return index
    return 0


__all__ = [
    "ProjectionDefinition",
    "ProjectionError",
    "ProjectionHandle",
    "ProjectionRegistry",
    "inherited_event_count",
]
