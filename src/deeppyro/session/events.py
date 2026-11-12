"""Session v4 event vocabulary, envelope validation, and surface metadata.

The canonical rules are reverse-engineered from DSH's @deepseek-ai/dsh-session
implementation.  Every persisted record is validated here before it enters a
:class:'deeppyro.session.Session'; the same validation gates appends so a log can
never be written in a shape the reader would refuse.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping, Sequence
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import (
    MESSAGE_PROJECTION_TYPES,
    RECORD_TYPES,
    SURFACE_EVENT_TYPES,
)

KNOWN_EVENT_TYPES: frozenset[str] = frozenset(RECORD_TYPES)
SURFACE_EVENTS: frozenset[str] = frozenset(SURFACE_EVENT_TYPES)
PROJECTION_EVENTS: frozenset[str] = frozenset(MESSAGE_PROJECTION_TYPES)

MESSAGE_ROLE_BY_TYPE: dict[str, str] = {
    "system/message": "system",
    "developer/message": "developer",
    "user/message": "user",
    "assistant/message": "assistant",
    "tool/result": "tool",
}
MESSAGE_EVENT_TYPES = frozenset(MESSAGE_ROLE_BY_TYPE)

HEADER_REQUIRED_KEYS = (
    "type",
    "version",
    "id",
    "createdAt",
    "isSeeded",
    "delegationDepth",
)
HEADER_OPTIONAL_KEYS = ("cwd", "parentSession", "origin", "agentPreset")

ENVELOPE_KEYS = frozenset({"type", "seq", "time", "data", "surfaceOp", "sourceEventSeqs", "ignorable"})

#: Envelope keys copied verbatim when re-serializing a record.
ENVELOPE_ORDER = ("type", "seq", "time", "data", "surfaceOp", "sourceEventSeqs", "ignorable")


def is_event_seq(value: object) -> bool:
    """Whether *value* is a non-negative, non-bool integer seq/time value."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_surface_type(event_type: str) -> bool:
    """Whether *event_type* enters the model-visible surface."""
    return event_type in SURFACE_EVENTS


def decode_source_event_seqs(value: object, *, max_seq: int) -> list[int]:
    """Decode sourceEventSeqs which may use [start, end] range pairs.

    Ranges must be strictly increasing, unique, and reference events earlier
    than *max_seq* (the current event's seq).  Mirrors DSH's decodeSeqRanges.
    """
    if not isinstance(value, list):
        raise SessionError("sourceEventSeqs must be an array")
    output: list[int] = []
    has_range = False
    for entry in value:
        if isinstance(entry, list):
            if len(entry) != 2:
                raise SessionError("sourceEventSeqs range must be a [start, end] pair")
            start, end = entry
            if not is_event_seq(start) or not is_event_seq(end):
                raise SessionError("sourceEventSeqs range bounds must be non-negative integers")
            if start > end:
                raise SessionError("sourceEventSeqs range start must not exceed its end")
            if end >= max_seq:
                raise SessionError(f"sourceEventSeqs range exceeds its event seq: {end} >= {max_seq}")
            output.extend(range(start, end + 1))
            has_range = True
        else:
            if not is_event_seq(entry):
                raise SessionError("sourceEventSeqs member must be a non-negative integer")
            output.append(entry)
    seen: set[int] = set()
    for source in output:
        if source >= max_seq:
            raise SessionError(
                f"sourceEventSeqs must reference earlier events: {source} >= current seq {max_seq}"
            )
        if source in seen:
            raise SessionError("sourceEventSeqs must be unique")
        seen.add(source)
    if has_range and any(output[index] <= output[index - 1] for index in range(1, len(output))):
        raise SessionError("sourceEventSeqs ranges must be strictly increasing")
    return output


def encode_source_event_seqs(values: Sequence[int]) -> list[int | list[int]]:
    """Encode a sorted seq list using DSH's compact [start, end] ranges.

    Runs of three or more consecutive seqs become a range pair; shorter runs
    stay as individual integers.  A non-increasing list is preserved verbatim
    (it will be rejected by the reader anyway).
    """
    seqs = list(values)
    if any(seqs[index] <= seqs[index - 1] for index in range(1, len(seqs))):
        return seqs
    output: list[int | list[int]] = []
    index = 0
    while index < len(seqs):
        start = seqs[index]
        end = start
        while index + 1 < len(seqs) and seqs[index + 1] == end + 1:
            index += 1
            end += 1
        if end - start >= 2:
            output.append([start, end])
        else:
            run_start = index - (end - start)
            output.extend(seqs[run_start : index + 1])
        index += 1
    return output


def validate_header(raw: Mapping[str, Any]) -> None:
    """Validate one session header row (line 0 of a session.v4.jsonl log)."""
    if not isinstance(raw, Mapping):
        raise SessionError("session header must be a JSON object")
    keys = set(raw)
    required = set(HEADER_REQUIRED_KEYS)
    allowed = required | set(HEADER_OPTIONAL_KEYS)
    missing = required - keys
    if missing:
        raise SessionError(f"session header lacks required field {min(missing)}")
    unexpected = keys - allowed
    if unexpected:
        raise SessionError(f"session header has unexpected field {min(unexpected)}")
    if raw["type"] != "session":
        raise SessionError('session header type must be "session"')
    if raw["version"] != 4:
        raise SessionError(f"session format is v4, got v{raw['version']!r}")
    if not isinstance(raw["id"], str) or not raw["id"]:
        raise SessionError("session header id must be a non-empty string")
    if not is_event_seq(raw["createdAt"]):
        raise SessionError("session header createdAt must be a non-negative integer")
    if not isinstance(raw["isSeeded"], bool):
        raise SessionError("session header isSeeded must be a boolean")
    if not is_event_seq(raw["delegationDepth"]):
        raise SessionError("session header delegationDepth must be a non-negative integer")
    cwd = raw.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd:
            raise SessionError("session header cwd must be a non-empty string")
        if not is_absolute_path(cwd):
            raise SessionError("session header cwd must be an absolute path")
    parent = raw.get("parentSession")
    if parent is not None and not isinstance(parent, str):
        raise SessionError("session header parentSession must be a string")
    origin = raw.get("origin")
    if origin is not None and origin != "subagent":
        raise SessionError('session header origin must be "subagent"')
    preset = raw.get("agentPreset")
    if preset is not None and not isinstance(preset, str):
        raise SessionError("session header agentPreset must be a string")


def is_absolute_path(value: str) -> bool:
    """Cross-platform absolute-path check matching Node's path.isAbsolute intent."""
    if value.startswith(("/", "\\")):
        return True
    try:
        return pathlib.Path(value).is_absolute()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return False


def header_line_dict(header: Mapping[str, Any]) -> dict[str, Any]:
    """Encode a logical header into the canonical first-line JSON object."""
    return {
        "type": "session",
        "version": 4,
        "id": header["id"],
        "createdAt": header["createdAt"],
        **({"cwd": header["cwd"]} if header.get("cwd") else {}),
        **({"parentSession": header["parentSession"]} if header.get("parentSession") is not None else {}),
        "isSeeded": bool(header.get("isSeeded", False)),
        **({"origin": header["origin"]} if header.get("origin") is not None else {}),
        "delegationDepth": header.get("delegationDepth", 0),
        **({"agentPreset": header["agentPreset"]} if header.get("agentPreset") is not None else {}),
    }


def normalize_event(raw: Mapping[str, Any], *, subject: str = "event") -> dict[str, Any]:
    """Validate one raw record and return its canonical Python form.

    The returned mapping has top-level sourceEventSeqs flattened to ints
    (range pairs decoded); every other field is copied unchanged.
    """
    if not isinstance(raw, Mapping):
        raise SessionError(f"{subject} must be a JSON object")
    unexpected = set(raw) - ENVELOPE_KEYS
    if unexpected:
        raise SessionError(f"{subject} has unexpected envelope field {min(unexpected)}")
    for key in ("type", "seq", "time", "data"):
        if key not in raw:
            raise SessionError(f"{subject} lacks required field {key}")
    event_type = raw["type"]
    if not isinstance(event_type, str) or not event_type:
        raise SessionError(f"{subject} type must be a non-empty string")
    if not is_event_seq(raw["seq"]):
        raise SessionError(f"{subject} seq must be a non-negative integer")
    if not is_event_seq(raw["time"]):
        raise SessionError(f"{subject} time must be a non-negative integer")
    if not isinstance(raw["data"], Mapping):
        raise SessionError(f"{subject} data must be a JSON object")
    seq = raw["seq"]
    ignorable = raw.get("ignorable")
    if ignorable is not None and ignorable is not True:
        raise SessionError(f"{subject} ignorable may only be true")

    known = event_type in KNOWN_EVENT_TYPES
    if not known:
        if ignorable is not True:
            raise SessionError(f"{subject} has unknown event type {event_type!r} and is not ignorable")
        if "surfaceOp" in raw or "sourceEventSeqs" in raw:
            raise SessionError(f"{subject} unknown ignorable event cannot carry surface metadata")
        return _copy_event(raw, None)

    is_surface = is_surface_type(event_type)
    surface_op = raw.get("surfaceOp")
    if is_surface:
        if surface_op is None:
            raise SessionError(f"{subject} {event_type} requires surfaceOp")
        if surface_op != "append":
            if not isinstance(surface_op, Mapping):
                raise SessionError(f'{subject} surfaceOp must be "append" or a replace object')
            if set(surface_op) != {"op", "startSeq", "endSeq"}:
                raise SessionError(f"{subject} replace surfaceOp must have exactly op/startSeq/endSeq")
            if surface_op["op"] != "replace":
                raise SessionError(f'{subject} surfaceOp op must be "replace"')
            if not is_event_seq(surface_op["startSeq"]) or not is_event_seq(surface_op["endSeq"]):
                raise SessionError(f"{subject} replace surfaceOp seq bounds must be non-negative integers")
            if surface_op["startSeq"] > surface_op["endSeq"]:
                raise SessionError(f"{subject} replace surfaceOp startSeq must not exceed endSeq")
            if surface_op["startSeq"] >= seq or surface_op["endSeq"] >= seq:
                raise SessionError(
                    f"surface replace at seq {seq}: startSeq and endSeq must reference earlier events"
                )
    else:
        if surface_op is not None:
            raise SessionError(f"{subject} {event_type} is not surface-eligible and cannot carry surfaceOp")
        if "sourceEventSeqs" in raw:
            raise SessionError(
                f"{subject} {event_type} is not surface-eligible and cannot carry sourceEventSeqs"
            )

    sources: list[int] | None = None
    if "sourceEventSeqs" in raw:
        if event_type == "assistant/message":
            raise SessionError(f"{subject} assistant/message must not carry sourceEventSeqs")
        sources = decode_source_event_seqs(raw["sourceEventSeqs"], max_seq=seq)
    _validate_event_data(raw, subject)
    return _copy_event(raw, sources)


def _copy_event(raw: Mapping[str, Any], sources: list[int] | None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "type": raw["type"],
        "seq": raw["seq"],
        "time": raw["time"],
        "data": dict(raw["data"]),
    }
    surface_op = raw.get("surfaceOp")
    if surface_op is not None:
        output["surfaceOp"] = surface_op
    if sources is not None:
        output["sourceEventSeqs"] = sources
    if raw.get("ignorable") is True:
        output["ignorable"] = True
    return output


def _validate_event_data(raw: Mapping[str, Any], subject: str) -> None:
    """Validate event-specific payload invariants required for safe replay."""
    event_type = raw["type"]
    data = raw["data"]
    if event_type in MESSAGE_EVENT_TYPES:
        message = data if event_type == "user/message" else data.get("message")
        if not isinstance(message, Mapping):
            raise SessionError(f"{subject} {event_type} lacks a message object")
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise SessionError(f"{subject} {event_type} lacks an identified message")
        expected_role = MESSAGE_ROLE_BY_TYPE[event_type]
        if message.get("role") != expected_role:
            raise SessionError(f"{subject} {event_type} message must have role {expected_role!r}")
        source = message.get("source")
        if not isinstance(source, Mapping) or not isinstance(source.get("kind"), str) or not source["kind"]:
            raise SessionError(f"{subject} {event_type} message has invalid source")
        if not isinstance(message.get("content"), list):
            raise SessionError(f"{subject} {event_type} message has invalid content")
        if event_type == "system/message" and source["kind"] != "system-prompt":
            raise SessionError(f"{subject} system/message message must have system-prompt source")
        if event_type == "assistant/message" and (
            source["kind"] != "model" or not _has_provider_model(source)
        ):
            raise SessionError(f"{subject} assistant/message message must have model source")
        if event_type == "tool/result":
            if source["kind"] != "tool" or not isinstance(source.get("callId"), str) or not source["callId"]:
                raise SessionError(f"{subject} tool/result message must have tool source")
            if message.get("toolCallId") != source["callId"]:
                raise SessionError(f"{subject} tool/result message has mismatched tool call ids")
            if data.get("error") is not None and message.get("isError") is not True:
                raise SessionError(f"{subject} tool/result error requires message.isError === true")
        if event_type == "developer/message" and message.get("role") != "developer":
            raise SessionError(f"{subject} developer/message and developer role must occur together")
        if message.get("role") != "developer":
            for block in message["content"]:
                if isinstance(block, Mapping) and block.get("type") in ("tool-addition", "tool-removal"):
                    raise SessionError(f"{subject} tool-change blocks require developer role")
        if event_type == "developer/message":
            _validate_developer_data(raw, subject)
    elif event_type == "request/header":
        header = data.get("header")
        if not isinstance(header, Mapping):
            raise SessionError(f"{subject} request/header header must be an object")
        if "system" in header:
            raise SessionError(f"{subject} request/header must omit header.system; use system/message")
        if isinstance(header.get("tools"), list) and not header["tools"]:
            raise SessionError(f"{subject} request/header must omit empty tools")
        defaults = header.get("adapterDefaults")
        if isinstance(defaults, Mapping) and not defaults:
            raise SessionError(f"{subject} request/header must omit empty adapterDefaults")


def _validate_developer_data(raw: Mapping[str, Any], subject: str) -> None:
    data = raw["data"]
    message = data["message"]
    has_additions = False
    for block in message["content"]:
        if not isinstance(block, Mapping) or block.get("type") not in ("tool-addition", "tool-removal"):
            continue
        tool_name = block.get("toolName")
        if not isinstance(tool_name, str) or not tool_name:
            raise SessionError(f"{subject} {block['type']} requires a nonempty toolName")
        if block["type"] == "tool-addition":
            has_additions = True
            if "tool" in block:
                raise SessionError(f"{subject} tool-addition must omit inline tool definitions")
    has_header_seq = "headerSeq" in data
    if has_additions != has_header_seq:
        raise SessionError(
            f"{subject} developer/message requires headerSeq exactly when tool additions are present"
        )


def _has_provider_model(source: Mapping[str, Any]) -> bool:
    provider = source.get("provider")
    model = source.get("model")
    return isinstance(provider, str) and bool(provider) and isinstance(model, str) and bool(model)


def assert_source_event_references(
    event: Mapping[str, Any], shadowed_seqs: Sequence[int], *, subject: str = "event"
) -> None:
    """Require a replacement's sourceEventSeqs to cover every shadowed node."""
    sources = event.get("sourceEventSeqs")
    if not sources:
        missing = list(shadowed_seqs)
    else:
        missing = [seq for seq in shadowed_seqs if seq not in sources]
    if missing:
        raise SessionError(
            f"{subject} surface replace: sourceEventSeqs must include every shadowed surface node; "
            f"missing {missing[0]}"
        )
