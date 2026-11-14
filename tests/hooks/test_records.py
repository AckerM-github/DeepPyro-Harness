"""Record shaping tests for hook observations (task-65)."""

from __future__ import annotations

from typing import Any

from deeppyro.hooks import HookObservation
from deeppyro.hooks.records import (
    HOT_HOOKS,
    INVOKED_RECORD_TYPE,
    MAX_PREVIEW,
    RESULT_RECORD_TYPE,
    agent_from_payload,
    invoked_record,
    is_journaled,
    result_record,
)
from deeppyro.hooks.types import (
    AGENT_ASSISTANT_STREAM,
    LLM_STREAM,
    TOOL_CALL,
    TOOLS_PRE_EXECUTE,
)


def observation(
    *,
    kind: str = "invoked",
    hook: str = TOOL_CALL,
    mode: str = "emit",
    scope: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    result: Any = None,
    error: BaseException | None = None,
    duration_ms: float = 0.0,
) -> HookObservation:
    return HookObservation(
        kind=kind,
        hook=hook,
        mode=mode,
        scope=scope,
        payload=payload or {},
        result=result,
        error=error,
        duration_ms=duration_ms,
    )


RICH_PAYLOAD: dict[str, Any] = {
    "agent": object(),
    "turn": 2,
    "step": 3,
    "tool": "read",
    "call": {"callId": "call-7", "name": "read", "arguments": {}},
    "provider": "local",
    "model": "test-model",
    "phase": "start",
    "status": "ok",
    "arguments": {"path": "a.txt"},
    "result": [1, 2],
}


class Agent:
    def record_hook_event(self, record_type: str, data: dict[str, Any]) -> int:
        return 1


def test_constants() -> None:
    assert INVOKED_RECORD_TYPE == "hook/invoked"
    assert RESULT_RECORD_TYPE == "hook/result"
    assert MAX_PREVIEW == 120
    assert HOT_HOOKS == frozenset({LLM_STREAM, AGENT_ASSISTANT_STREAM})


def test_is_journaled_skips_hot_hooks() -> None:
    assert is_journaled(TOOL_CALL) is True
    assert is_journaled(TOOLS_PRE_EXECUTE) is True
    assert is_journaled(LLM_STREAM) is False
    assert is_journaled(AGENT_ASSISTANT_STREAM) is False


def test_invoked_record_has_fixed_key_order() -> None:
    record = invoked_record(
        observation(
            kind="invoked",
            hook=TOOL_CALL,
            mode="emit",
            scope={"session": "session-1", "preset": "default", "other": "ignored"},
            payload=RICH_PAYLOAD,
        )
    )

    assert list(record) == [
        "hook",
        "mode",
        "scope",
        "turn",
        "step",
        "tool",
        "callId",
        "provider",
        "model",
        "phase",
        "status",
    ]
    assert record == {
        "hook": TOOL_CALL,
        "mode": "emit",
        "scope": {"session": "session-1", "preset": "default"},
        "turn": 2,
        "step": 3,
        "tool": "read",
        "callId": "call-7",
        "provider": "local",
        "model": "test-model",
        "phase": "start",
        "status": "ok",
    }


def test_invoked_record_without_optional_data() -> None:
    record = invoked_record(observation(payload={}))

    assert list(record) == ["hook", "mode"]
    assert record == {"hook": TOOL_CALL, "mode": "emit"}


def test_invoked_record_only_keeps_primitive_fields() -> None:
    record = invoked_record(
        observation(
            payload={
                "turn": 2,
                "step": True,
                "tool": 3.5,
                "provider": None,
                "model": {"nested": "ignored"},
                "phase": [1, 2],
                "status": object(),
            }
        )
    )

    assert record["turn"] == 2
    assert record["step"] is True
    assert "tool" not in record
    assert "provider" not in record
    assert "model" not in record
    assert "phase" not in record
    assert "status" not in record


def test_call_id_comes_from_the_nested_call_only() -> None:
    direct = invoked_record(observation(payload={"callId": "direct"}))
    nested = invoked_record(observation(payload={"call": {"callId": "nested"}}))
    broken = invoked_record(observation(payload={"call": "not-a-mapping"}))

    assert "callId" not in direct
    assert nested["callId"] == "nested"
    assert "callId" not in broken


def test_scope_only_keeps_session_and_preset_primitives() -> None:
    record = invoked_record(observation(scope={"session": 7, "preset": True, "agent": "ignored"}))
    invalid = invoked_record(observation(scope={"session": ["not", "primitive"], "preset": 1.5}))

    assert record["scope"] == {"session": 7, "preset": True}
    assert "scope" not in invalid


def test_result_record_for_a_string_value() -> None:
    record = result_record(observation(kind="resolved", result="allow", duration_ms=12.9))

    assert list(record) == ["hook", "mode", "ok", "outcome", "durationMs", "preview"]
    assert record == {
        "hook": TOOL_CALL,
        "mode": "emit",
        "ok": True,
        "outcome": "value",
        "durationMs": 12,
        "preview": "allow",
    }


def test_result_record_for_a_structured_value() -> None:
    record = result_record(observation(kind="resolved", result={"kind": "allow"}, duration_ms=1.0))

    assert record["ok"] is True
    assert record["outcome"] == "value"
    assert record["valueType"] == "dict"
    assert record["preview"] == '{"kind": "allow"}'


def test_result_record_for_none() -> None:
    record = result_record(observation(kind="resolved", result=None))

    assert record["ok"] is True
    assert record["outcome"] == "none"
    assert "preview" not in record
    assert "valueType" not in record


def test_result_record_for_an_error() -> None:
    record = result_record(observation(kind="resolved", error=RuntimeError("boom"), duration_ms=3.2))

    assert record["ok"] is False
    assert record["outcome"] == "error"
    assert record["durationMs"] == 3
    assert record["error"] == {"name": "RuntimeError", "message": "boom"}
    assert "preview" not in record
    assert "valueType" not in record


def test_result_preview_truncates_to_max_preview() -> None:
    record = result_record(observation(kind="resolved", result="x" * 500))

    assert record["preview"] == "x" * MAX_PREVIEW
    assert len(record["preview"]) == MAX_PREVIEW


def test_result_preview_handles_non_json_values() -> None:
    record = result_record(observation(kind="resolved", result=object()))

    assert record["valueType"] == "object"
    assert record["preview"]
    assert len(record["preview"]) <= MAX_PREVIEW


def test_result_preview_keeps_unicode() -> None:
    record = result_record(observation(kind="resolved", result={"text": "中文"}))

    assert record["preview"] == '{"text": "中文"}'


def test_result_record_includes_optional_fields_without_scope() -> None:
    record = result_record(
        observation(
            kind="resolved",
            hook=TOOLS_PRE_EXECUTE,
            mode="waterfall",
            scope={"session": "session-1"},
            payload=RICH_PAYLOAD,
            result={"kind": "allow"},
            duration_ms=0.4,
        )
    )

    assert list(record) == [
        "hook",
        "mode",
        "ok",
        "outcome",
        "durationMs",
        "valueType",
        "preview",
        "turn",
        "step",
        "tool",
        "callId",
        "provider",
        "model",
        "phase",
        "status",
    ]
    assert "scope" not in record
    assert record["tool"] == "read"
    assert record["callId"] == "call-7"
    assert record["durationMs"] == 0


def test_agent_from_payload_finds_a_journaling_agent() -> None:
    agent = Agent()

    assert agent_from_payload({"agent": agent}) is agent
    assert agent_from_payload({"agent": object()}) is None
    assert agent_from_payload({"agent": None}) is None
    assert agent_from_payload({}) is None


def test_invoked_and_result_records_are_plain_dicts() -> None:
    invoked = invoked_record(observation())
    result = result_record(observation(kind="resolved"))

    assert type(invoked) is dict
    assert type(result) is dict
