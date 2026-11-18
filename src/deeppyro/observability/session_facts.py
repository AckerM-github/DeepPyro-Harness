"""Session-record facts shared by the v4 usage and trace reporters.

The v4 agent emits its facts as durable session records (`assistant/message`
carries token usage, `turn/end` carries the stop reason) and as `llm/stream`
hook frames (which carry attempt boundaries but no usage). This module turns
those two streams into immutable turn/call facts so the usage ledger and the
trace reporter agree on one interpretation instead of each re-deriving it.

Reading is incremental and idempotent: a session already on disk when the
service starts is skipped (only new records are accounted), and a turn is
emitted at most once — first on `turn/end`, or earlier when the caller forces
the turn (the v4 `agent/turn-stopping` hook fires just before `turn/end`).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from deeppyro.core.ids import now_ms

__all__ = [
    "ModelCallFact",
    "RecordSession",
    "SessionRecordScanner",
    "StepAttempts",
    "StreamAttemptTracker",
    "TurnFact",
]

#: Provider finish kinds (kebab-case) mapped to the native trace vocabulary.
NATIVE_FINISH = {
    "stop": "stop",
    "tool-calls": "tool_calls",
    "length": "length",
    "error": "error",
    "aborted": "aborted",
    "content-filter": "content_filter",
}

#: Turn reasons that are not a plain completion mapped to a stop word.
_TURN_REASON = {
    "aborted": "aborted",
    "error": "error",
    "max-steps": "length",
    "max-tokens": "length",
}


class RecordSession(Protocol):
    """The slice of a v4 session handle the reporter needs."""

    @property
    def session_id(self) -> str:
        """Stable session id used as the accounting key."""
        ...

    @property
    def records(self) -> Sequence[Any]:
        """Append-only log records (`LogRecord` envelopes)."""
        ...


@dataclass(slots=True)
class StepAttempts:
    """Attempt bookkeeping for one (turn, step) provider call."""

    attempts: int = 0
    started: float = 0.0
    latency_ms: int = 0
    finish_reason: str = "stop"

    @property
    def retries(self) -> int:
        """Number of retries: all attempts after the first."""
        return max(self.attempts - 1, 0)


class StreamAttemptTracker:
    """Tracks `llm/stream` start/end frames into per-step attempt statistics."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, int, int], StepAttempts] = {}
        self._active: dict[str, tuple[int, int]] = {}

    def observe(self, session_id: str, frame: Mapping[str, Any]) -> None:
        """Fold one `llm/stream` frame (start/end/chunk) into the attempt state.

        The v4 `start` frame carries turn/step while the matching `end` frame
        only carries the finish kind, so the end frame is attributed to the
        step the session most recently started.
        """
        frame_type = frame.get("type")
        turn = _as_int(frame.get("turn"))
        step = _as_int(frame.get("step"))
        if turn is None or step is None:
            active = self._active.get(session_id)
            if frame_type != "end" or active is None:
                return
            turn, step = active
        key = (session_id, turn, step)
        if frame_type == "start":
            self._active[session_id] = (turn, step)
            state = self._states.get(key)
            if state is None:
                state = StepAttempts()
                self._states[key] = state
            state.attempts += 1
            state.started = time.monotonic()
        elif frame_type == "end":
            state = self._states.get(key)
            if state is None:
                return
            state.latency_ms = max(0, int((time.monotonic() - state.started) * 1000.0))
            finish = frame.get("finish")
            if isinstance(finish, str):
                state.finish_reason = NATIVE_FINISH.get(finish, finish.replace("-", "_"))

    def stats(self, session_id: str, turn: int, step: int) -> StepAttempts | None:
        """Current attempt statistics for one step, or None when never observed."""
        return self._states.get((session_id, turn, step))

    def forget_turn(self, session_id: str, turn: int) -> None:
        """Drop the bookkeeping for one turn once its fact has been emitted."""
        for key in [key for key in self._states if key[0] == session_id and key[1] == turn]:
            del self._states[key]
        if self._active.get(session_id, (None,))[0] == turn:
            self._active.pop(session_id, None)

    def forget_session(self, session_id: str) -> None:
        """Drop every step for one session (session disposal)."""
        for key in [key for key in self._states if key[0] == session_id]:
            del self._states[key]
        self._active.pop(session_id, None)


@dataclass(frozen=True, slots=True)
class ModelCallFact:
    """One completed v4 model call: tokens, latency, attempts and finish kind."""

    turn: int
    step: int
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    reasoning_tokens: int | None
    finish_reason: str
    retries: int
    latency_ms: int
    ts: int
    message_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnFact:
    """One completed v4 turn aggregated from its model calls."""

    session_id: str
    turn: int
    provider: str
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    retries: int
    ts: int
    calls: tuple[ModelCallFact, ...]


class SessionRecordScanner:
    """Incrementally turns v4 session records into :class:`TurnFact` objects."""

    def __init__(self, attempts: StreamAttemptTracker | None = None) -> None:
        self._attempts = attempts if attempts is not None else StreamAttemptTracker()
        self._index: dict[str, int] = {}
        self._started: set[str] = set()
        self._pending: dict[str, dict[int, list[ModelCallFact]]] = {}
        self._emitted: set[tuple[str, int]] = set()

    @property
    def attempts(self) -> StreamAttemptTracker:
        """The attempt tracker fed by :meth:`observe_frame`."""
        return self._attempts

    def observe_frame(self, session_id: str, frame: Mapping[str, Any]) -> None:
        """Fold one `llm/stream` frame into the per-step attempt statistics."""
        self._attempts.observe(session_id, frame)

    def scan(
        self, session: RecordSession, *, force_turn: int | None = None, force_all: bool = False
    ) -> list[TurnFact]:
        """Return turn facts discovered since the previous scan.

        Args:
            session: The v4 session handle whose records are read.
            force_turn: Emit this turn now even though `turn/end` is not yet
                appended (the v4 turn-stopping hook runs just before it).
            force_all: Emit every buffered turn (session disposal).
        """
        session_id = session.session_id
        records = session.records
        if session_id not in self._started:
            self._started.add(session_id)
            self._index[session_id] = len(records)
            return []
        start = self._index.get(session_id, 0)
        pending = self._pending.setdefault(session_id, {})
        results: list[TurnFact] = []
        for record in records[start:]:
            record_type = getattr(record, "type", None)
            data = getattr(record, "data", None)
            if not isinstance(data, Mapping):
                continue
            if record_type == "assistant/message":
                call = self._call_fact(session_id, data, getattr(record, "time", 0))
                if call is not None and (session_id, call.turn) not in self._emitted:
                    pending.setdefault(call.turn, []).append(call)
            elif record_type == "turn/end":
                turn = _as_int(data.get("turn"))
                if turn is None:
                    continue
                calls = pending.pop(turn, [])
                if (session_id, turn) not in self._emitted:
                    fact = self._turn_fact(session_id, turn, calls, data.get("reason"))
                    if fact is not None:
                        self._emitted.add((session_id, turn))
                        results.append(fact)
        self._index[session_id] = len(records)
        if force_turn is not None or force_all:
            turns = [force_turn] if force_turn is not None else list(pending)
            for turn in turns:
                if turn is None or (session_id, turn) in self._emitted:
                    pending.pop(turn, None)
                    continue
                calls = pending.pop(turn, [])
                if not calls:
                    continue
                fact = self._turn_fact(session_id, turn, calls, None)
                if fact is not None:
                    self._emitted.add((session_id, turn))
                    results.append(fact)
        return results

    def forget_session(self, session_id: str) -> None:
        """Forget all scanner state for a disposed session."""
        self._index.pop(session_id, None)
        self._started.discard(session_id)
        self._pending.pop(session_id, None)
        self._emitted = {key for key in self._emitted if key[0] != session_id}
        self._attempts.forget_session(session_id)

    # -- internals -----------------------------------------------------
    def _call_fact(self, session_id: str, data: Mapping[str, Any], record_time: int) -> ModelCallFact | None:
        turn = _as_int(data.get("turn"))
        step = _as_int(data.get("step"))
        usage = data.get("usage")
        if turn is None or step is None or not isinstance(usage, Mapping):
            return None
        provider, model = _source_of(data.get("message"))
        tokens = _tokens(usage)
        stats = self._attempts.stats(session_id, turn, step)
        finish = stats.finish_reason if stats is not None else "stop"
        message = data.get("message")
        message_id = message.get("id") if isinstance(message, Mapping) else None
        return ModelCallFact(
            turn=turn,
            step=step,
            provider=provider,
            model=model,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cached_tokens=tokens[2],
            reasoning_tokens=_as_int_or_none(usage.get("reasoningTokens")),
            finish_reason=finish,
            retries=stats.retries if stats is not None else 0,
            latency_ms=stats.latency_ms if stats is not None else 0,
            ts=max(int(record_time or 0), 0),
            message_id=message_id if isinstance(message_id, str) else None,
        )

    def _turn_fact(
        self, session_id: str, turn: int, calls: Sequence[ModelCallFact], reason: Any
    ) -> TurnFact | None:
        if not calls:
            return None
        last = calls[-1]
        stop_reason = _stop_reason(reason, last.finish_reason)
        self._attempts.forget_turn(session_id, turn)
        return TurnFact(
            session_id=session_id,
            turn=turn,
            provider=last.provider,
            model=last.model,
            stop_reason=stop_reason,
            input_tokens=sum(call.input_tokens for call in calls),
            output_tokens=sum(call.output_tokens for call in calls),
            cached_tokens=sum(call.cached_tokens for call in calls),
            latency_ms=sum(call.latency_ms for call in calls),
            retries=sum(call.retries for call in calls),
            ts=now_ms(),
            calls=tuple(calls),
        )


def _stop_reason(reason: Any, fallback: str) -> str:
    """Map a v4 `turn/end` reason to a native-style stop word."""
    if not isinstance(reason, Mapping):
        return fallback
    kind = reason.get("kind")
    if isinstance(kind, str):
        if kind in _TURN_REASON:
            return _TURN_REASON[kind]
        if kind != "completed":
            return kind
    return fallback


def _tokens(usage: Mapping[str, Any]) -> tuple[int, int, int]:
    """Return (input, output, cached) from the v4 camelCase usage mapping."""
    return (
        _as_int(usage.get("inputTokens")) or 0,
        _as_int(usage.get("outputTokens")) or 0,
        _as_int(usage.get("cacheReadTokens")) or 0,
    )


def _source_of(message: Any) -> tuple[str, str]:
    """Return (provider, model) from an assistant message envelope."""
    if isinstance(message, Mapping):
        source = message.get("source")
        if isinstance(source, Mapping):
            provider = source.get("provider")
            model = source.get("model")
            return (
                provider if isinstance(provider, str) else "",
                model if isinstance(model, str) else "",
            )
    return "", ""


def _as_int(value: Any) -> int | None:
    """Coerce an int-like value, returning None for bools and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _as_int_or_none(value: Any) -> int | None:
    """Coerce an optional non-negative int."""
    number = _as_int(value)
    if number is None or number < 0:
        return None
    return number
