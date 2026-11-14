"""Shape hook observations into replayable session records.

The session journal stores two record types, hook/invoked and hook/result. The
shapes are deeppyro-specific: they align with the DSH vocabulary names but do
not promise byte-level compatibility. Both functions return plain dicts with a
fixed key order so golden tests and log readers stay stable.

Only primitive payload values are copied: mappings, lists, and custom objects
stay out of the record. The full frozen payload remains available to observers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .types import AGENT_ASSISTANT_STREAM, LLM_STREAM

if TYPE_CHECKING:
    from .service import HookObservation

INVOKED_RECORD_TYPE = "hook/invoked"
RESULT_RECORD_TYPE = "hook/result"

#: Hooks dispatched once per frame on the streaming hot path. Journaling them
#: would write two session records per model frame, so they are excluded.
HOT_HOOKS = frozenset({LLM_STREAM, AGENT_ASSISTANT_STREAM})

#: Maximum characters kept in the recorded preview of a hook result.
MAX_PREVIEW = 120

_SCOPE_KEYS = ("session", "preset")
_FIELD_KEYS = ("turn", "step", "tool", "callId", "provider", "model", "phase", "status")
_PRIMITIVES = (str, int, bool)
_MISSING = object()


def invoked_record(observation: HookObservation) -> dict[str, Any]:
    """Build the hook/invoked record for one observation.

    Keys are ordered hook, mode, then the optional scope and the primitive
    payload fields. Fields that are absent, None, or not primitive are omitted.
    """
    record: dict[str, Any] = {"hook": observation.hook, "mode": observation.mode}
    scope = _scope_record(observation.scope)
    if scope:
        record["scope"] = scope
    _append_fields(record, observation.payload)
    return record


def result_record(observation: HookObservation) -> dict[str, Any]:
    """Build the hook/result record for one resolved observation.

    outcome is "error" when the observation carries an error, "none" when the
    dispatch produced no value, and "value" otherwise. valueType and preview
    only describe an actual result; error is the exception name and message.
    """
    error = observation.error
    result = observation.result
    if error is not None:
        outcome = "error"
    elif result is None:
        outcome = "none"
    else:
        outcome = "value"
    record: dict[str, Any] = {
        "hook": observation.hook,
        "mode": observation.mode,
        "ok": error is None,
        "outcome": outcome,
        "durationMs": int(observation.duration_ms),
    }
    if result is not None and not isinstance(result, str):
        record["valueType"] = type(result).__name__
    preview = _preview(result)
    if preview is not None:
        record["preview"] = preview
    if error is not None:
        record["error"] = {"name": type(error).__name__, "message": str(error)}
    _append_fields(record, observation.payload)
    return record


def agent_from_payload(payload: Mapping[str, Any]) -> Any | None:
    """Return payload["agent"] when it exposes a callable record_hook_event."""
    if not isinstance(payload, Mapping):
        return None
    agent = payload.get("agent")
    if agent is None:
        return None
    if callable(getattr(agent, "record_hook_event", None)):
        return agent
    return None


def is_journaled(hook: str) -> bool:
    """Return True when the hook is safe to write to a session journal."""
    return hook not in HOT_HOOKS


def _scope_record(scope: Mapping[str, Any] | None) -> dict[str, Any]:
    if scope is None:
        return {}
    record: dict[str, Any] = {}
    for key in _SCOPE_KEYS:
        if key not in scope:
            continue
        value = scope[key]
        if isinstance(value, _PRIMITIVES):
            record[key] = value
    return record


def _append_fields(record: dict[str, Any], payload: Mapping[str, Any]) -> None:
    for key in _FIELD_KEYS:
        value = _payload_value(payload, key)
        if value is not _MISSING:
            record[key] = value


def _payload_value(payload: Mapping[str, Any], key: str) -> Any:
    if key == "callId":
        call = payload.get("call")
        if not isinstance(call, Mapping):
            return _MISSING
        value = call.get("callId")
    else:
        value = payload.get(key)
    if value is None or not isinstance(value, _PRIMITIVES):
        return _MISSING
    return value


def _preview(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = repr(result)
    return text[:MAX_PREVIEW]


__all__ = [
    "HOT_HOOKS",
    "INVOKED_RECORD_TYPE",
    "MAX_PREVIEW",
    "RESULT_RECORD_TYPE",
    "agent_from_payload",
    "invoked_record",
    "is_journaled",
    "result_record",
]
