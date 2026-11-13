"""Assistant stream timing records.

DSH stores one provider stream twice: the live chunks drive the UI, and a compacted,
time-stamped form is persisted as assistant/message.stream (see
fixtures/records/by-type/assistant_message.jsonl).  The persisted form groups
consecutive deltas of the same kind into one record so a 200-chunk answer costs a
handful of JSON objects:

    {"type": "text-chunks", "time0": 1794054684030, "index": 1,
     "dt": [0, 0, 8, ...], "texts": ["你好", "！我是", ...]}

dt[i] is the gap in milliseconds between delta i and delta i + 1; time0 is the
arrival time of the first delta.  A delta that cannot be merged with the previous
record starts a new record, and anything else (block-start, block-end, usage,
finish, unknown chunk types) is stored verbatim as a "chunk" record.

The accumulator mirrors @deepseek-ai/dsh-llm's AssistantStreamAccumulator: push one
timed chunk and get back the timed value; snapshot() returns the JSON-serializable
stream array.  One deliberate deviation: DSH's safeGap() accepts negative gaps as
long as they round-trip exactly, while the DeepPyro contract only merges
non-negative gaps (a wall clock is monotonic in practice, and a negative gap means
the clock was adjusted, which should stay visible as a new record).  It also splits
after MAX_MERGE_GAP_MS (60 s) so a stalled stream stays visible in replay.

Example:
    acc = AssistantStreamAccumulator()
    acc.push({"type": "text-delta", "index": 0, "text": "Hel"})
    acc.push({"type": "text-delta", "index": 0, "text": "lo"})
    acc.snapshot()
    # [{"type": "text-chunks", "time0": ..., "index": 0, "dt": [..], "texts": ["Hel", "lo"]}]
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from deeppyro.core.ids import now_ms

# JavaScript's Number.MAX_SAFE_INTEGER: the largest timestamp the accumulator accepts.
MAX_SAFE_INT = 2**53 - 1

# Longest gap (ms) still folded into one chunks record.  DSH merges any safe integer
# gap; DeepPyro splits after a minute so a stalled stream stays visible in replay and
# dt arrays cannot grow without bound.  A gap at or below this value merges.
MAX_MERGE_GAP_MS = 60_000

_TEXT_RECORD_TYPES = {"text-delta": "text-chunks", "reasoning-delta": "reasoning-chunks"}
_CHUNK_RECORD_TYPES = ("text-chunks", "reasoning-chunks", "tool-call-chunks")


class AssistantStreamAccumulator:
    """Turn timed assistant stream chunks into durable timing records.

    The clock is injected so tests can drive exact timings; production code uses
    deeppyro.core.ids.now_ms.  push() takes the clock exactly once per chunk, so a
    merged delta costs no extra time lookup and time0 + dt always reproduces the
    delta's own arrival time.
    """

    __slots__ = ("_clock", "_records")

    def __init__(self, *, clock: Callable[[], int] | None = None) -> None:
        self._clock = clock if clock is not None else now_ms
        self._records: list[dict[str, Any]] = []

    def push(self, chunk: Mapping[str, Any]) -> dict[str, Any]:
        """Record one chunk and return its timed form {"time": int, "chunk": dict}.

        Raises TypeError for a chunk that is not JSON-serializable and ValueError
        for structurally invalid deltas (bad index, non-string text, empty tool
        call id or name).
        """
        if not isinstance(chunk, Mapping):
            raise TypeError(f"assistant stream chunk must be a mapping, got {type(chunk).__name__}")
        time = self._now()
        snapshot = _snapshot_chunk(chunk)
        chunk_type = snapshot.get("type")
        if not isinstance(chunk_type, str):
            raise TypeError("assistant stream chunk.type must be a string")
        previous = self._records[-1] if self._records else None
        record_type = _TEXT_RECORD_TYPES.get(chunk_type)
        if record_type is not None:
            self._push_text(previous, time, chunk_type, record_type, snapshot)
        elif chunk_type == "tool-call-delta":
            self._push_tool_call(previous, time, snapshot)
        else:
            self._records.append({"type": "chunk", "time": time, "chunk": snapshot})
        return {"time": time, "chunk": _clone_json(snapshot)}

    def snapshot(self) -> list[dict[str, Any]]:
        """Return the persisted stream records (no internal lastTime).

        Every call returns fresh, deeply detached dicts: mutating the result never
        affects the accumulator or a later snapshot.
        """
        records: list[dict[str, Any]] = []
        for record in self._records:
            if record["type"] == "chunk":
                records.append(
                    {"type": "chunk", "time": record["time"], "chunk": _clone_json(record["chunk"])}
                )
                continue
            durable = {key: value for key, value in record.items() if key != "lastTime"}
            durable["dt"] = list(record["dt"])
            if record["type"] == "tool-call-chunks":
                durable["args"] = list(record["args"])
            else:
                durable["texts"] = list(record["texts"])
            records.append(durable)
        return records

    # -- internals ---------------------------------------------------------
    def _now(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"clock must return an int, got {type(value).__name__}")
        if value < 0 or value > MAX_SAFE_INT:
            raise ValueError(f"clock returned {value}, expected 0..{MAX_SAFE_INT}")
        return value

    def _push_text(
        self,
        previous: dict[str, Any] | None,
        time: int,
        chunk_type: str,
        record_type: str,
        chunk: Mapping[str, Any],
    ) -> None:
        index = _require_index(chunk.get("index"), chunk_type)
        text = _require_str(chunk.get("text"), f"{chunk_type}.text")
        gap = _merge_gap(previous, record_type, time)
        if (
            previous is not None
            and previous["type"] == record_type
            and previous["index"] == index
            and gap is not None
        ):
            previous["dt"].append(gap)
            previous["texts"].append(text)
            previous["lastTime"] = time
            return
        self._records.append(
            {
                "type": record_type,
                "time0": time,
                "index": index,
                "dt": [],
                "texts": [text],
                "lastTime": time,
            }
        )

    def _push_tool_call(
        self,
        previous: dict[str, Any] | None,
        time: int,
        chunk: Mapping[str, Any],
    ) -> None:
        # Fragments merge only when the id, the name (including its presence) and the
        # gap all agree, so a feed must either attach the name to every fragment or
        # to none of them; mixing starts a new record.
        index = _require_index(chunk.get("index"), "tool-call-delta")
        call_id = _require_str(chunk.get("id"), "tool-call-delta.id")
        if not call_id:
            raise ValueError("tool-call-delta.id must not be empty")
        has_name = "name" in chunk
        name: str | None = None
        if has_name:
            name = _require_str(chunk.get("name"), "tool-call-delta.name")
            if not name:
                raise ValueError("tool-call-delta.name must not be empty")
        delta = _require_str(chunk.get("argumentsDelta"), "tool-call-delta.argumentsDelta")
        gap = _merge_gap(previous, "tool-call-chunks", time)
        if (
            previous is not None
            and previous["type"] == "tool-call-chunks"
            and previous["index"] == index
            and previous["id"] == call_id
            and ("name" in previous) == has_name
            and previous.get("name") == name
            and gap is not None
        ):
            previous["dt"].append(gap)
            previous["args"].append(delta)
            previous["lastTime"] = time
            return
        record: dict[str, Any] = {
            "type": "tool-call-chunks",
            "time0": time,
            "index": index,
            "dt": [],
            "id": call_id,
        }
        if has_name:
            record["name"] = name
        record["args"] = [delta]
        record["lastTime"] = time
        self._records.append(record)


def expand_stream(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the timed chunks that produced records.

    Inverse of AssistantStreamAccumulator.snapshot(): the first delta of a record
    arrives at time0 and each following delta dt[i] milliseconds later.  Records
    that are already "chunk" entries keep their stored time.
    """
    timed: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise TypeError(f"assistant stream record must be a mapping, got {type(record).__name__}")
        record_type = record.get("type")
        if record_type == "chunk":
            time = _require_time(record.get("time"), "chunk.time")
            timed.append({"time": time, "chunk": _clone_json(record.get("chunk"))})
            continue
        if record_type not in _CHUNK_RECORD_TYPES:
            raise ValueError(f"unknown assistant stream record type: {record_type!r}")
        time0 = _require_time(record.get("time0"), f"{record_type}.time0")
        gaps = record.get("dt")
        if not isinstance(gaps, Sequence) or isinstance(gaps, (str, bytes)):
            raise TypeError(f"{record_type}.dt must be a sequence")
        index = _require_index(record.get("index"), str(record_type))
        values_name = "args" if record_type == "tool-call-chunks" else "texts"
        values = record.get(values_name)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise TypeError(f"{record_type}.{values_name} must be a sequence")
        if len(gaps) != len(values) - 1:
            raise ValueError(
                f"{record_type}.dt must have exactly one gap fewer than values "
                f"({len(gaps)} != {len(values) - 1})"
            )
        time = time0
        for position, value in enumerate(values):
            if position:
                time = _advance(time, gaps[position - 1])
            if record_type == "tool-call-chunks":
                chunk: dict[str, Any] = {
                    "type": "tool-call-delta",
                    "index": index,
                    "id": _require_str(record.get("id"), "tool-call-chunks.id"),
                }
                if "name" in record:
                    chunk["name"] = _require_str(record.get("name"), "tool-call-chunks.name")
                chunk["argumentsDelta"] = _require_str(value, "tool-call-chunks.args")
            else:
                chunk = {
                    "type": "text-delta" if record_type == "text-chunks" else "reasoning-delta",
                    "index": index,
                    "text": _require_str(value, f"{record_type}.texts"),
                }
            timed.append({"time": time, "chunk": chunk})
    return timed


def _merge_gap(previous: dict[str, Any] | None, record_type: str, time: int) -> int | None:
    """Return the gap to append to previous, or None when a new record is required."""
    if previous is None or previous["type"] != record_type:
        return None
    gap = time - previous["lastTime"]
    if 0 <= gap <= MAX_MERGE_GAP_MS:
        return gap
    return None


def _snapshot_chunk(chunk: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _clone_json(chunk)
    except (TypeError, ValueError) as exc:
        raise TypeError("assistant stream chunk must be losslessly JSON-serializable") from exc


def _clone_json(value: Any) -> Any:
    return json.loads(json.dumps(value, allow_nan=False))


def _require_time(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < 0 or value > MAX_SAFE_INT:
        raise ValueError(f"{name} must be between 0 and {MAX_SAFE_INT}, got {value}")
    return value


def _require_index(value: Any, name: str) -> int:
    return _require_time(value, f"{name}.index")


def _require_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string, got {type(value).__name__}")
    return value


def _advance(time: int, gap: Any) -> int:
    gap_value = _require_time(gap, "dt gap")
    result = time + gap_value
    if result > MAX_SAFE_INT:
        raise ValueError(f"expanded stream time exceeds {MAX_SAFE_INT}")
    return result
