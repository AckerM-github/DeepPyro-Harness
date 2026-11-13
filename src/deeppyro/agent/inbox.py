"""Durable agent inbox: pending next-turn / next-step input.

The inbox is a projection over one durable record type, 'agent/inbox/spliced'.
Every mutation normalizes the requested range, checks that message ids stay
unique across both pending lists, appends one splice record, and only then
publishes process-local notifications. Replaying those records reconstructs
the exact pending input after a restart.

Semantics mirror DSH ReactLoopInbox / the standard inbox projection:

* 'next-turn' holds prompts that each deserve a turn of their own.
* 'next-step' holds steering input consumed at the next step boundary.
* public removals are cancellations: they persist outcome 'canceled' and emit
  'agent/inbox/discarded' for every removed message;
* claiming is a pure deletion (no outcome, no discarded event) and emits
  'agent/inbox/claimed' for every message in the returned batch;
* every insertion emits 'agent/inbox/inserted'.

The inbox never writes surface records and never renders messages; callers wire
it into AgentSession. 'log' only needs an 'append(type, data) -> int' method
(deeppyro.session.Session and SessionLogLike both qualify).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import Message

TARGET_NEXT_TURN = "next-turn"
TARGET_NEXT_STEP = "next-step"
TARGETS: tuple[str, ...] = (TARGET_NEXT_TURN, TARGET_NEXT_STEP)

SPLICED_EVENT = "agent/inbox/spliced"
INSERTED_EVENT = "agent/inbox/inserted"
DISCARDED_EVENT = "agent/inbox/discarded"
CLAIMED_EVENT = "agent/inbox/claimed"

EmitCallback = Callable[[str, dict[str, Any]], None]

_MAX_SAFE_INTEGER = 2**53 - 1


def _dump(message: Message) -> dict[str, Any]:
    """Return the persisted wire shape used in splice records and events."""
    return message.model_dump(exclude_none=True)


def _record_field(record: Any, name: str) -> Any:
    """Read a field from a durable record (model object or plain mapping)."""
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def _is_safe_integer(value: Any) -> bool:
    """Whether value is an integer JSON/JS can address exactly."""
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= _MAX_SAFE_INTEGER


def _trunc_number(value: Any) -> int | float:
    """Mirror JavaScript Math.trunc including NaN -> 0 and infinities.

    None and unparsable values become 0, matching Math.trunc(undefined) in the
    DSH implementation. Infinities are returned unchanged so the caller can
    clamp them against the queue length.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return 0
        if math.isinf(value):
            return value
        return math.trunc(value)
    try:
        return _trunc_number(float(value))
    except (TypeError, ValueError):
        return 0


def _normalize_splice(length: int, start: Any, delete_count: Any) -> tuple[int, int]:
    """Resolve splice coordinates the way DSH's mutate does."""
    offset = _trunc_number(start)
    if isinstance(offset, float):  # only +/-infinity reaches this branch
        actual_start = length if offset > 0 else 0
    elif offset < 0:
        actual_start = max(length + offset, 0)
    else:
        actual_start = min(offset, length)

    count = _trunc_number(delete_count)
    if isinstance(count, float):  # +/-infinity
        actual_delete = length - actual_start if count > 0 else 0
    else:
        actual_delete = min(max(count, 0), length - actual_start)
    return actual_start, actual_delete


def _require_message(message: Any) -> Message:
    """Validate one inbox message."""
    if not isinstance(message, Message):
        raise TypeError("inbox messages must be deeppyro.core.wire.Message instances")
    return message


def _require_messages(inserted: Any) -> list[Message]:
    """Validate a sequence of inbox messages."""
    if isinstance(inserted, (Message, str, bytes)):
        raise TypeError("inserted must be a sequence of Message objects")
    if not isinstance(inserted, Sequence):
        raise TypeError("inserted must be a sequence of Message objects")
    return [_require_message(message) for message in inserted]


def _assert_unique(messages: Iterable[Message]) -> None:
    """Reject a duplicate message id across the combined pending lists."""
    ids: set[str] = set()
    for message in messages:
        if message.id in ids:
            raise SessionError(f'message "{message.id}" is already pending')
        ids.add(message.id)


class Inbox:
    """Durable pending-input queues for one agent session.

    Args:
        log: Session log (or any object with 'append(type, data) -> int').
            None keeps the inbox in memory only, which is useful in tests and
            when replaying detached records.
        emit: Process-local notification callback '(event_type, payload)'.
    """

    def __init__(self, log: Any | None = None, *, emit: EmitCallback | None = None) -> None:
        self._log = log
        self._emit_callback = emit
        self._state: dict[str, list[Message]] = {target: [] for target in TARGETS}

    # -- construction from durable history --------------------------------
    @classmethod
    def from_records(
        cls,
        records: Iterable[Any],
        log: Any | None = None,
        emit: EmitCallback | None = None,
    ) -> Inbox:
        """Rebuild both queues by replaying 'agent/inbox/spliced' records.

        Other event types are ignored. Persisted splices are validated more
        strictly than live mutations (start and removedCount must be exact
        integers inside the current queue); a bad record raises SessionError
        mentioning its session seq.
        """
        inbox = cls(log, emit=emit)
        for record in records:
            if _record_field(record, "type") != SPLICED_EVENT:
                continue
            seq = _record_field(record, "seq")
            data = _record_field(record, "data")
            try:
                inbox._apply_splice(data)
            except Exception as exc:
                raise SessionError(f"invalid persisted inbox splice at session seq {seq}") from exc
        return inbox

    # -- read-only views ---------------------------------------------------
    @property
    def next_turn(self) -> tuple[Message, ...]:
        """Prompts each waiting for their own turn."""
        return tuple(self._state[TARGET_NEXT_TURN])

    @property
    def next_step(self) -> tuple[Message, ...]:
        """Input waiting for the next step boundary."""
        return tuple(self._state[TARGET_NEXT_STEP])

    @property
    def has_pending(self) -> bool:
        """Whether either queue contains work."""
        return bool(self._state[TARGET_NEXT_TURN] or self._state[TARGET_NEXT_STEP])

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Return both queues as detached persisted-wire dictionaries."""
        return {target: [_dump(message) for message in self._state[target]] for target in TARGETS}

    # -- claiming ----------------------------------------------------------
    def claim(self, target: str, turn: int) -> list[Message]:
        """Remove and return the batch proposed for one step boundary.

        All next-step input is claimed first, then one queued next-turn prompt
        when target is next-turn. Each message emits 'agent/inbox/claimed'
        with the owning turn. Empty queues write no records; an unknown target
        raises ValueError before mutating.
        """
        self._require_target(target)
        claimed = self._mutate(TARGET_NEXT_STEP, 0, len(self._state[TARGET_NEXT_STEP]), [], False)
        if target == TARGET_NEXT_TURN:
            claimed.extend(self._mutate(TARGET_NEXT_TURN, 0, 1, [], False))
        for message in claimed:
            self._emit_event(CLAIMED_EVENT, {"message": _dump(message), "turn": turn})
        return claimed

    def clear(self) -> None:
        """Durably cancel all pending input, clearing next-step before next-turn."""
        self.splice(TARGET_NEXT_STEP, 0, len(self._state[TARGET_NEXT_STEP]), [], True)
        self.splice(TARGET_NEXT_TURN, 0, len(self._state[TARGET_NEXT_TURN]), [], True)

    # -- insertion and mutation -------------------------------------------
    def push(self, target: str, message: Message, *, front: bool = False) -> None:
        """Insert one message at the front or back of target."""
        self._require_target(target)
        validated = _require_message(message)
        index = 0 if front else len(self._state[target])
        self._mutate(target, index, 0, [validated], False)

    def append(self, target: str, message: Message) -> None:
        """Append one message (convenience for push(..., front=False))."""
        self.push(target, message)

    def prepend(self, target: str, message: Message) -> None:
        """Prepend one message (convenience for push(..., front=True))."""
        self.push(target, message, front=True)

    def splice(
        self,
        target: str,
        start: Any,
        delete_count: Any,
        inserted: Sequence[Message],
        discard_removed: bool = False,
    ) -> list[Message]:
        """Apply one normalized splice, persist it, and return removed messages.

        Coordinates follow JavaScript Array.prototype.splice / Math.trunc:
        fractional values truncate, NaN becomes 0, negative starts count from
        the end, and the delete count clamps to the queue. A no-op (nothing
        deleted, nothing inserted) writes no record. discard_removed marks the
        deletion as a durable cancellation (outcome 'canceled') and emits a
        discarded event per message.
        """
        self._require_target(target)
        messages = _require_messages(inserted)
        return self._mutate(target, start, delete_count, messages, discard_removed)

    def remove(self, message_id: str) -> list[Message]:
        """Cancel one pending message by id, searching next-turn then next-step."""
        location = self._locate(message_id)
        if location is None:
            return []
        target, index = location
        return self.splice(target, index, 1, [], True)

    def replace(self, message_id: str, new_message: Message) -> bool:
        """Replace one pending message in place, returning whether it existed."""
        location = self._locate(message_id)
        if location is None:
            return False
        target, index = location
        self.splice(target, index, 1, [_require_message(new_message)], True)
        return True

    # -- internals ---------------------------------------------------------
    def _require_target(self, target: str) -> None:
        if target not in self._state:
            raise ValueError(f"unknown inbox target: {target!r}")

    def _locate(self, message_id: str) -> tuple[str, int] | None:
        for target in TARGETS:
            for index, message in enumerate(self._state[target]):
                if message.id == message_id:
                    return target, index
        return None

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._emit_callback is not None:
            self._emit_callback(event_type, payload)

    def _mutate(
        self,
        target: str,
        start: Any,
        delete_count: Any,
        inserted: Sequence[Message],
        discard_removed: bool,
    ) -> list[Message]:
        """Normalize, validate, persist, commit, then publish one mutation."""
        inbox = self._state[target]
        actual_start, actual_delete = _normalize_splice(len(inbox), start, delete_count)
        if actual_delete == 0 and not inserted:
            return []
        candidate = inbox[:actual_start] + list(inserted) + inbox[actual_start + actual_delete :]
        other = self._state[TARGET_NEXT_STEP if target == TARGET_NEXT_TURN else TARGET_NEXT_TURN]
        combined = candidate + list(other) if target == TARGET_NEXT_TURN else list(other) + candidate
        _assert_unique(combined)

        removed = inbox[actual_start : actual_start + actual_delete]
        outcome = "canceled" if discard_removed and actual_delete > 0 else None
        data: dict[str, Any] = {"target": target, "start": actual_start}
        if actual_delete:
            data["removedCount"] = actual_delete
        data["inserted"] = [_dump(message) for message in inserted]
        if outcome is not None:
            data["outcome"] = outcome

        if self._log is not None:
            self._log.append(SPLICED_EVENT, data)
        self._state[target] = candidate

        if discard_removed:
            for message in removed:
                self._emit_event(DISCARDED_EVENT, {"message": _dump(message)})
        for message in inserted:
            self._emit_event(INSERTED_EVENT, {"message": _dump(message), "target": target})
        return removed

    def _apply_splice(self, data: Any) -> None:
        """Apply one persisted splice with projection-level strictness."""
        if not isinstance(data, Mapping):
            raise TypeError("inbox splice data must be a mapping")
        target = data.get("target")
        if target not in self._state:
            raise ValueError(f"unknown inbox target: {target!r}")
        inbox = self._state[target]
        start = data.get("start")
        removed_count = data.get("removedCount")
        removed_count = 0 if removed_count is None else removed_count
        if not _is_safe_integer(start) or start < 0 or start > len(inbox):
            raise ValueError("invalid inbox splice start")
        if not _is_safe_integer(removed_count) or removed_count < 0 or start + removed_count > len(inbox):
            raise ValueError("invalid inbox splice removedCount")
        raw_inserted = data.get("inserted")
        if not isinstance(raw_inserted, list):
            raise TypeError("invalid inbox splice inserted")
        inserted = [Message.model_validate(item) for item in raw_inserted]

        candidate = inbox[:start] + inserted + inbox[start + removed_count :]
        other = self._state[TARGET_NEXT_STEP if target == TARGET_NEXT_TURN else TARGET_NEXT_TURN]
        combined = candidate + list(other) if target == TARGET_NEXT_TURN else list(other) + candidate
        _assert_unique(combined)
        self._state[target] = candidate


__all__ = [
    "CLAIMED_EVENT",
    "DISCARDED_EVENT",
    "INSERTED_EVENT",
    "SPLICED_EVENT",
    "TARGETS",
    "TARGET_NEXT_STEP",
    "TARGET_NEXT_TURN",
    "Inbox",
]
