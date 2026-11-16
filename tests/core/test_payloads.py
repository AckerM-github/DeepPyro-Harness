"""Shape tests for the typed payload dicts (lane: payload-typedict).

These tests lock the dictionary shapes that cross module boundaries.  The key
set and insertion order of these dicts are part of the on-disk/wire format
(JSON serialization preserves insertion order), so the assertions compare
ordered key lists, not just membership.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.core.payloads import SessionEvent, SessionEventLine, ToolCallPayload
from deeppyro.core.protocols import ToolResult
from deeppyro.core.wire import LogRecord
from deeppyro.session.events import header_line_dict, normalize_event
from deeppyro.session.writer import event_to_dict
from deeppyro.tools.pipeline import ToolCallRequest, _call_payload, run_tool_batch
from deeppyro.tools.registry import ToolRegistry
from tests.tools.helpers import make_context


class RecordingHooks:
    """Capture the three tools/* payloads and allow every decision."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}
        self.scopes: dict[str, Any] = {}

    async def waterfall(
        self,
        name: str,
        payload: dict[str, Any],
        fallback: Any = None,
        *,
        scope: Any = None,
    ) -> Any:
        self.payloads[name] = dict(payload)
        self.scopes[name] = scope
        if name == "tools/pre-execute":
            return {"kind": "allow"}
        if name == "tools/execute":
            return ToolResult(text="hooked")
        return {"kind": "accept"}


def request() -> ToolCallRequest:
    return ToolCallRequest(call_id="c1", name="echo", arguments='{"value": 1}')


def round_trip(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False))


async def run(hooks: RecordingHooks, **kwargs: Any) -> list[Any]:
    return await run_tool_batch(
        [request()],
        registry=ToolRegistry([SimpleNamespace(name="echo", parallel_safe=True)]),
        ctx_factory=lambda req: make_context(Path.cwd(), call_id=req.call_id),
        hooks=hooks,
        turn=1,
        step=2,
        **kwargs,
    )


def test_tool_call_payload_matches_the_wire_shape() -> None:
    payload: ToolCallPayload = _call_payload(request())
    assert list(payload) == ["callId", "name", "arguments"]
    assert payload == {"callId": "c1", "name": "echo", "arguments": '{"value": 1}'}
    assert round_trip(payload) == payload


async def test_tool_hook_payloads_lock_key_order_and_json_round_trip() -> None:
    hooks = RecordingHooks()
    outcomes = await run(hooks)
    assert outcomes[0].result.text == "hooked"

    pre = hooks.payloads["tools/pre-execute"]
    execute = hooks.payloads["tools/execute"]
    post = hooks.payloads["tools/post-execute"]

    assert list(pre) == ["call", "tool", "turn", "step", "agent"]
    assert list(execute) == ["call", "tool", "turn", "step", "arguments", "agent"]
    assert list(post) == ["call", "tool", "turn", "step", "result", "agent"]
    assert list(pre["call"]) == ["callId", "name", "arguments"]
    assert (pre["tool"], pre["turn"], pre["step"]) == ("echo", 1, 2)
    assert execute["arguments"] == {"value": 1}
    assert isinstance(post["result"], ToolResult)

    for payload in (pre, execute):
        assert round_trip(payload) == payload


async def test_agent_and_tool_set_flow_into_context_and_scope() -> None:
    hooks = RecordingHooks()
    agent = SimpleNamespace(session_id="s1")
    await run(hooks, agent=agent, tool_set=["echo", "read"])

    assert hooks.payloads["tools/pre-execute"]["agent"] is agent
    assert hooks.payloads["tools/pre-execute"]["toolSet"] == ["echo", "read"]
    assert hooks.scopes["tools/pre-execute"] == {"session": "s1"}
    assert hooks.scopes["tools/post-execute"] == {"session": "s1"}

    bare = RecordingHooks()
    await run(bare)
    assert bare.payloads["tools/pre-execute"]["agent"] is None
    assert "toolSet" not in bare.payloads["tools/pre-execute"]
    assert bare.scopes["tools/pre-execute"] is None

    preset = RecordingHooks()
    await run(preset, agent=SimpleNamespace(session_id="s1", preset="reviewer"))
    assert preset.scopes["tools/execute"] == {"session": "s1", "preset": "reviewer"}


def test_header_line_locks_canonical_key_order_and_json() -> None:
    line = header_line_dict(
        {
            "id": "s1",
            "createdAt": 5,
            "cwd": "C:/work",
            "parentSession": "parent",
            "origin": "subagent",
            "agentPreset": "reviewer",
            "isSeeded": True,
            "delegationDepth": 2,
        }
    )
    assert list(line) == [
        "type",
        "version",
        "id",
        "createdAt",
        "cwd",
        "parentSession",
        "isSeeded",
        "origin",
        "delegationDepth",
        "agentPreset",
    ]
    assert line == {
        "type": "session",
        "version": 4,
        "id": "s1",
        "createdAt": 5,
        "cwd": "C:/work",
        "parentSession": "parent",
        "isSeeded": True,
        "origin": "subagent",
        "delegationDepth": 2,
        "agentPreset": "reviewer",
    }
    assert json.dumps(line, ensure_ascii=False, separators=(",", ":")) == (
        '{"type":"session","version":4,"id":"s1","createdAt":5,"cwd":"C:/work",'
        '"parentSession":"parent","isSeeded":true,"origin":"subagent","delegationDepth":2,'
        '"agentPreset":"reviewer"}'
    )


def test_session_event_envelope_round_trips_through_writer() -> None:
    raw = {
        "type": "tool/result",
        "seq": 4,
        "time": 7,
        "data": {
            "message": {
                "id": "m1",
                "role": "tool",
                "toolCallId": "c1",
                "source": {"kind": "tool", "callId": "c1"},
                "content": [],
            }
        },
        "surfaceOp": {"op": "replace", "startSeq": 1, "endSeq": 2},
        "sourceEventSeqs": [0, [1, 2]],
    }
    normalized: SessionEvent = normalize_event(raw)
    assert list(normalized) == ["type", "seq", "time", "data", "surfaceOp", "sourceEventSeqs"]
    assert normalized["sourceEventSeqs"] == [0, 1, 2]

    encoded: SessionEventLine = event_to_dict(LogRecord.model_validate(normalized))
    assert list(encoded) == ["type", "seq", "time", "data", "surfaceOp", "sourceEventSeqs"]
    assert encoded["surfaceOp"] == {"op": "replace", "startSeq": 1, "endSeq": 2}
    assert encoded["sourceEventSeqs"] == [[0, 2]]
    assert json.loads(json.dumps(encoded, ensure_ascii=False)) == encoded
