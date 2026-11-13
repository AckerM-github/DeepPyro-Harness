"""Tests for the assistant stream timing accumulator and its inverse."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.stream import (
    MAX_MERGE_GAP_MS,
    MAX_SAFE_INT,
    AssistantStreamAccumulator,
    expand_stream,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
ASSISTANT_MESSAGE = FIXTURES / "records" / "by-type" / "assistant_message.jsonl"


def clock_from(times: list[int]) -> Callable[[], int]:
    iterator: Iterator[int] = iter(times)

    def clock() -> int:
        return next(iterator)

    return clock


def text_delta(text: str, index: int = 0) -> dict[str, Any]:
    return {"type": "text-delta", "index": index, "text": text}


def reasoning_delta(text: str, index: int = 0) -> dict[str, Any]:
    return {"type": "reasoning-delta", "index": index, "text": text}


def tool_delta(index: int, call_id: str, arguments: str, *, name: str | None = None) -> dict[str, Any]:
    chunk: dict[str, Any] = {
        "type": "tool-call-delta",
        "index": index,
        "id": call_id,
        "argumentsDelta": arguments,
    }
    if name is not None:
        chunk["name"] = name
    return chunk


def fixture_stream() -> list[dict[str, Any]]:
    line = ASSISTANT_MESSAGE.read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)["data"]["stream"]


def test_consecutive_text_deltas_merge() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([1000, 1010, 1032]))
    acc.push(text_delta("Hel"))
    acc.push(text_delta("lo"))
    acc.push(text_delta("!"))
    assert acc.snapshot() == [
        {
            "type": "text-chunks",
            "time0": 1000,
            "index": 0,
            "dt": [10, 22],
            "texts": ["Hel", "lo", "!"],
        }
    ]


def test_zero_gap_still_merges() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([5, 5, 5]))
    for text in ("a", "b", "c"):
        acc.push(text_delta(text))
    assert acc.snapshot() == [
        {"type": "text-chunks", "time0": 5, "index": 0, "dt": [0, 0], "texts": ["a", "b", "c"]}
    ]


def test_negative_gap_starts_new_record() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([1000, 990]))
    acc.push(text_delta("before"))
    acc.push(text_delta("after"))
    assert acc.snapshot() == [
        {"type": "text-chunks", "time0": 1000, "index": 0, "dt": [], "texts": ["before"]},
        {"type": "text-chunks", "time0": 990, "index": 0, "dt": [], "texts": ["after"]},
    ]


def test_gap_at_cap_merges() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([0, MAX_MERGE_GAP_MS]))
    acc.push(text_delta("slow"))
    acc.push(text_delta("delta"))
    assert acc.snapshot() == [
        {
            "type": "text-chunks",
            "time0": 0,
            "index": 0,
            "dt": [MAX_MERGE_GAP_MS],
            "texts": ["slow", "delta"],
        }
    ]


def test_gap_above_cap_starts_new_record() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([0, MAX_MERGE_GAP_MS + 1]))
    acc.push(text_delta("slow"))
    acc.push(text_delta("delta"))
    snapshot = acc.snapshot()
    assert len(snapshot) == 2
    assert snapshot[1]["time0"] == MAX_MERGE_GAP_MS + 1
    assert snapshot[1]["dt"] == []


def test_index_change_starts_new_record() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([10, 20]))
    acc.push(text_delta("one", index=0))
    acc.push(text_delta("two", index=1))
    assert [record["index"] for record in acc.snapshot()] == [0, 1]


def test_reasoning_and_text_records_are_independent() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([100, 110, 120, 130]))
    acc.push(reasoning_delta("think"))
    acc.push(reasoning_delta("ing", index=0))
    acc.push(text_delta("hel", index=1))
    acc.push(text_delta("lo", index=1))
    assert acc.snapshot() == [
        {"type": "reasoning-chunks", "time0": 100, "index": 0, "dt": [10], "texts": ["think", "ing"]},
        {"type": "text-chunks", "time0": 120, "index": 1, "dt": [10], "texts": ["hel", "lo"]},
    ]


def test_raw_chunk_breaks_a_text_run() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([100, 105, 200]))
    acc.push(text_delta("before"))
    acc.push({"type": "block-start", "index": 1, "blockType": "text"})
    acc.push(text_delta("after", index=1))
    assert acc.snapshot() == [
        {"type": "text-chunks", "time0": 100, "index": 0, "dt": [], "texts": ["before"]},
        {
            "type": "chunk",
            "time": 105,
            "chunk": {"type": "block-start", "index": 1, "blockType": "text"},
        },
        {"type": "text-chunks", "time0": 200, "index": 1, "dt": [], "texts": ["after"]},
    ]


def test_tool_call_fragments_merge() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([10, 12, 15]))
    acc.push(tool_delta(2, "call_00_abc", "", name="read"))
    acc.push(tool_delta(2, "call_00_abc", '{"path"', name="read"))
    acc.push(tool_delta(2, "call_00_abc", ': "a.txt"}', name="read"))
    assert acc.snapshot() == [
        {
            "type": "tool-call-chunks",
            "time0": 10,
            "index": 2,
            "dt": [2, 3],
            "id": "call_00_abc",
            "name": "read",
            "args": ["", '{"path"', ': "a.txt"}'],
        }
    ]


def test_tool_call_without_name_merges() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([10, 20]))
    acc.push(tool_delta(0, "call_1", "{}"))
    acc.push(tool_delta(0, "call_1", ""))
    assert acc.snapshot() == [
        {
            "type": "tool-call-chunks",
            "time0": 10,
            "index": 0,
            "dt": [20 - 10],
            "id": "call_1",
            "args": ["{}", ""],
        }
    ]


def test_tool_call_name_change_starts_new_record() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([10, 20]))
    acc.push(tool_delta(0, "call_1", "", name="read"))
    acc.push(tool_delta(0, "call_1", "", name="write"))
    snapshot = acc.snapshot()
    assert len(snapshot) == 2
    assert [record.get("name") for record in snapshot] == ["read", "write"]


def test_tool_call_name_presence_change_starts_new_record() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([10, 20]))
    acc.push(tool_delta(0, "call_1", "", name="read"))
    acc.push(tool_delta(0, "call_1", "{}"))
    snapshot = acc.snapshot()
    assert len(snapshot) == 2
    assert "name" in snapshot[0]
    assert "name" not in snapshot[1]


def test_tool_call_id_change_starts_new_record() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([10, 20]))
    acc.push(tool_delta(0, "call_1", "", name="read"))
    acc.push(tool_delta(0, "call_2", "", name="read"))
    snapshot = acc.snapshot()
    assert len(snapshot) == 2
    assert [record["id"] for record in snapshot] == ["call_1", "call_2"]


def test_usage_and_finish_chunks_keep_order() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([100, 200, 300]))
    acc.push({"type": "usage", "usage": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12}})
    acc.push({"type": "finish", "reason": {"kind": "stop"}})
    acc.push({"type": "block-end", "index": 0, "block": {"type": "text", "text": "hi"}})
    assert [record["chunk"]["type"] for record in acc.snapshot()] == ["usage", "finish", "block-end"]
    assert [record["time"] for record in acc.snapshot()] == [100, 200, 300]


def test_snapshot_is_detached_and_json_serializable() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([1, 2]))
    acc.push(text_delta("hi"))
    acc.push({"type": "finish", "reason": {"kind": "stop"}})
    first = acc.snapshot()
    first[0]["texts"].append("mutated")
    first[0]["dt"].append(999)
    first[1]["chunk"]["reason"]["kind"] = "error"
    second = acc.snapshot()
    assert second[0]["texts"] == ["hi"]
    assert second[0]["dt"] == []
    assert second[1]["chunk"]["reason"]["kind"] == "stop"
    assert json.dumps(second, ensure_ascii=False)


def test_push_returns_detached_timed_value() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([42]))
    chunk = text_delta("hi")
    timed = acc.push(chunk)
    assert timed == {"time": 42, "chunk": chunk}
    timed["chunk"]["text"] = "mutated"
    chunk["text"] = "also mutated"
    assert acc.snapshot()[0]["texts"] == ["hi"]


def test_clock_called_once_per_push() -> None:
    calls: list[int] = []

    def clock() -> int:
        calls.append(len(calls))
        return 1000 + len(calls)

    acc = AssistantStreamAccumulator(clock=clock)
    acc.push(text_delta("a"))
    acc.push(text_delta("b"))
    acc.push({"type": "finish", "reason": {"kind": "stop"}})
    assert len(calls) == 3
    assert acc.snapshot()[0]["dt"] == [1]


def test_invalid_index_raises() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([1, 2, 3, 4, 5]))
    with pytest.raises(TypeError):
        acc.push({"type": "text-delta", "index": "0", "text": "x"})
    with pytest.raises(TypeError):
        acc.push({"type": "text-delta", "index": True, "text": "x"})
    with pytest.raises(ValueError):
        acc.push({"type": "text-delta", "index": -1, "text": "x"})
    with pytest.raises(TypeError):
        acc.push({"type": "text-delta", "text": "x"})
    assert acc.snapshot() == []


def test_invalid_text_raises() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([1, 2]))
    with pytest.raises(TypeError):
        acc.push({"type": "text-delta", "index": 0, "text": 5})
    with pytest.raises(TypeError):
        acc.push({"type": "reasoning-delta", "index": 0})
    assert acc.snapshot() == []


def test_invalid_tool_delta_raises() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([1, 2, 3, 4, 5]))
    with pytest.raises(ValueError):
        acc.push(tool_delta(0, "", "{}"))
    with pytest.raises(TypeError):
        acc.push({"type": "tool-call-delta", "index": 0, "id": 5, "argumentsDelta": "{}"})
    with pytest.raises(ValueError):
        acc.push(tool_delta(0, "call_1", "{}", name=""))
    with pytest.raises(TypeError):
        acc.push({"type": "tool-call-delta", "index": 0, "id": "call_1", "name": 3, "argumentsDelta": "{}"})
    with pytest.raises(TypeError):
        acc.push({"type": "tool-call-delta", "index": 0, "id": "call_1"})
    assert acc.snapshot() == []


def test_non_json_chunk_and_non_mapping_rejected() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([1, 2, 3]))
    with pytest.raises(TypeError):
        acc.push({"type": "usage", "usage": object()})
    with pytest.raises(TypeError):
        acc.push(["not", "a", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        acc.push({"text": "no type"})
    assert acc.snapshot() == []


def test_invalid_clock_raises() -> None:
    def bad_type() -> str:
        return "1"

    def negative() -> int:
        return -1

    def too_large() -> int:
        return MAX_SAFE_INT + 1

    with pytest.raises(TypeError):
        AssistantStreamAccumulator(clock=bad_type).push({"type": "finish"})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        AssistantStreamAccumulator(clock=negative).push({"type": "finish"})
    with pytest.raises(ValueError):
        AssistantStreamAccumulator(clock=too_large).push({"type": "finish"})


def test_expand_stream_is_the_inverse_of_snapshot() -> None:
    acc = AssistantStreamAccumulator(clock=clock_from([10, 13, 13]))
    acc.push(text_delta("a"))
    acc.push(text_delta("b"))
    acc.push({"type": "block-end", "index": 0, "block": {"type": "text", "text": "ab"}})
    expanded = expand_stream(acc.snapshot())
    assert expanded == [
        {"time": 10, "chunk": {"type": "text-delta", "index": 0, "text": "a"}},
        {"time": 13, "chunk": {"type": "text-delta", "index": 0, "text": "b"}},
        {"time": 13, "chunk": {"type": "block-end", "index": 0, "block": {"type": "text", "text": "ab"}}},
    ]


def test_expand_stream_rebuilds_tool_calls() -> None:
    records = [
        {
            "type": "tool-call-chunks",
            "time0": 100,
            "index": 2,
            "dt": [5],
            "id": "call_9",
            "name": "read",
            "args": ["{}", ""],
        },
        {
            "type": "tool-call-chunks",
            "time0": 200,
            "index": 3,
            "dt": [],
            "id": "call_10",
            "args": ["{}"],
        },
    ]
    first = {"type": "tool-call-delta", "index": 2, "id": "call_9", "name": "read", "argumentsDelta": "{}"}
    second = {"type": "tool-call-delta", "index": 2, "id": "call_9", "name": "read", "argumentsDelta": ""}
    third = {"type": "tool-call-delta", "index": 3, "id": "call_10", "argumentsDelta": "{}"}
    assert expand_stream(records) == [
        {"time": 100, "chunk": first},
        {"time": 105, "chunk": second},
        {"time": 200, "chunk": third},
    ]


def test_fixture_stream_round_trips_exactly() -> None:
    stream = fixture_stream()
    assert len(stream) == 8
    expanded = expand_stream(stream)
    acc = AssistantStreamAccumulator(clock=clock_from([item["time"] for item in expanded]))
    for item in expanded:
        acc.push(item["chunk"])
    assert acc.snapshot() == stream


def test_expand_stream_rejects_bad_records() -> None:
    with pytest.raises(TypeError):
        expand_stream(["not a record"])  # type: ignore[list-item]
    with pytest.raises(ValueError):
        expand_stream([{"type": "unknown"}])
    with pytest.raises(ValueError):
        expand_stream([{"type": "text-chunks", "time0": 1, "index": 0, "dt": [1, 2], "texts": ["x"]}])
    with pytest.raises(ValueError):
        expand_stream(
            [{"type": "text-chunks", "time0": MAX_SAFE_INT, "index": 0, "dt": [1], "texts": ["x", "y"]}]
        )
    with pytest.raises(TypeError):
        expand_stream([{"type": "text-chunks", "time0": 1, "index": 0, "dt": 0, "texts": ["x"]}])
    with pytest.raises(TypeError):
        expand_stream([{"type": "chunk", "chunk": {"type": "finish"}}])
