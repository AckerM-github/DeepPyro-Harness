"""One goal per session, recorded in the log and announced on the bus.

DSH keeps a goal as a session record (``goal/change``) plus a bus event
(``goal/changed``) so both the loop prompt and any observer can see it. This
service mirrors that: the record is the durable truth, the state dict is the
cache, and :meth:`get` rehydrates from the log when a session is reopened.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from deeppyro.core.errors import ToolError

logger = logging.getLogger(__name__)

#: Durable record written whenever the goal changes.
GOAL_EVENT = "goal/change"
#: Bus event published alongside it.
GOAL_BUS_EVENT = "goal/changed"

RecordFn = Callable[[str, dict[str, Any]], Any]


def _session_id(session: Any) -> str:
    if isinstance(session, str):
        if not session:
            raise ToolError("goal: session id must not be empty")
        return session
    if isinstance(session, Mapping):
        value = session.get("session_id") or session.get("sessionId")
    else:
        value = getattr(session, "session_id", None)
        if value is None:
            value = getattr(session, "sessionId", None)
    if not value:
        raise ToolError("goal: session has no session_id")
    return str(value)


def _records(session: Any) -> list[Any]:
    records = getattr(session, "records", None)
    if records is None:
        records = getattr(session, "log", None)
    return list(records or ())


def _record_type(record: Any) -> str:
    if isinstance(record, Mapping):
        return str(record.get("type") or "")
    return str(getattr(record, "type", "") or "")


def _record_data(record: Any) -> Mapping[str, Any]:
    data = record.get("data") if isinstance(record, Mapping) else getattr(record, "data", None)
    return data if isinstance(data, Mapping) else {}


class GoalService:
    """Per-session goal state; usable without a plugin context."""

    provide: ClassVar[str] = "goal"
    inject: ClassVar[list[str]] = ["sessions"]

    def __init__(self, config: Any = None) -> None:
        self._goals: dict[str, str] = {}
        self._records: dict[str, RecordFn] = {}
        self._emit: Callable[[str, dict[str, Any]], Any] | None = None
        #: Loads the durable records of one session so a reopened session
        #: rehydrates its goal without the host rebinding anything.
        self._loader: Callable[[str], list[Any]] | None = None
        self._pending: set[asyncio.Task[Any]] = set()
        self._max_chars = self._read_limit(config)

    @staticmethod
    def _read_limit(config: Any) -> int:
        value = None
        if isinstance(config, Mapping):
            value = config.get("maxChars", config.get("max_chars"))
        elif config is not None:
            value = getattr(config, "max_chars", None)
        try:
            limit = int(value) if value is not None else 2000
        except (TypeError, ValueError):
            return 2000
        return limit if limit > 0 else 2000

    # -- host wiring ---------------------------------------------------
    def configure(
        self,
        *,
        emit: Callable[[str, dict[str, Any]], Any] | None = None,
        record: RecordFn | None = None,
        load: Callable[[str], list[Any]] | None = None,
    ) -> None:
        if emit is not None:
            self._emit = emit
        if record is not None:
            self._records[""] = record
        if load is not None:
            self._loader = load

    def bind(self, session: Any, *, record: RecordFn | None = None) -> None:
        session_id = _session_id(session)
        if record is not None:
            self._records[session_id] = record
        elif hasattr(session, "append"):
            self._records[session_id] = session.append

    def unbind(self, session: Any) -> None:
        session_id = _session_id(session)
        self._goals.pop(session_id, None)
        self._records.pop(session_id, None)

    # -- state ---------------------------------------------------------
    def get(self, session: Any) -> str | None:
        """The current goal, or None; rehydrates from the log once."""
        session_id = _session_id(session)
        if session_id in self._goals:
            return self._goals[session_id]
        goal = self._from_records(session)
        if goal is not None:
            self._goals[session_id] = goal
        return goal

    def _from_records(self, session: Any) -> str | None:
        found: str | None = None
        if isinstance(session, str):
            if self._loader is None:
                return None
            loaded = self._loader(session)
        else:
            loaded = _records(session)
        for record in loaded:
            if _record_type(record) != GOAL_EVENT:
                continue
            value = _record_data(record).get("goal")
            found = str(value) if value else None
        return found

    async def set(self, session: Any, text: str) -> str:
        """Commit a new goal and announce it."""
        goal = " ".join(str(text).split())
        if not goal:
            raise ToolError("goal: text must not be empty")
        if len(goal) > self._max_chars:
            raise ToolError(f"goal: text must be at most {self._max_chars} characters")
        self.bind(session)
        session_id = _session_id(session)
        record = self._record_for(session_id)
        record(GOAL_EVENT, {"goal": goal})
        self._goals[session_id] = goal
        self._publish(session_id, goal)
        return goal

    async def clear(self, session: Any) -> None:
        """Drop the goal; the record keeps the history."""
        self.bind(session)
        session_id = _session_id(session)
        if session_id not in self._goals and self._from_records(session) is None:
            return
        record = self._record_for(session_id)
        record(GOAL_EVENT, {"goal": None})
        self._goals.pop(session_id, None)
        self._publish(session_id, None)

    def context_text(self, session: Any) -> str:
        """One prompt line for the active goal (empty when there is none)."""
        goal = self.get(session)
        return f"Current goal: {goal}." if goal else ""

    # -- internals -----------------------------------------------------
    def _record_for(self, session_id: str) -> RecordFn:
        record = self._records.get(session_id) or self._records.get("")
        if record is None:
            raise ToolError("goal: the service is not bound to this session")
        return record

    def _publish(self, session_id: str, goal: str | None) -> None:
        emitter = self._emit
        if emitter is None:
            return
        try:
            pending = emitter(GOAL_BUS_EVENT, {"session": session_id, "goal": goal})
        except Exception:  # pragma: no cover - observability must not fail a set
            logger.warning("failed to publish goal/changed", exc_info=True)
            return
        if not inspect.isawaitable(pending):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(pending)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)


__all__ = ["GOAL_BUS_EVENT", "GOAL_EVENT", "GoalService"]
