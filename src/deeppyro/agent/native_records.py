"""Shared native record and event conversion.

The native loop and the native host both persist facts as session events. Tool
records arrive with a v4 record type (for example "working-directory/change")
while new native code can append a schema EventKind directly. This module is the
one place that maps the former to the latter and the one place both append
helpers live, so the loop's tool log and the host's handle cannot drift.

It is wire-free and owns no state: RECORD_KIND_MAP is the exact seven legacy
record types the native host already accepted (adding a row is a schema change,
not an additive refactor), and every append helper resolves the event kind and
payload before touching the session, so an unsupported record type never leaves
a partial write behind.
"""

from __future__ import annotations

import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from deeppyro.core.messages import MessageSource, PluginSource, UserSource
from deeppyro.session.schema import Event, EventKind, NativeSession

__all__ = [
    "RECORD_KIND_MAP",
    "RecordMapping",
    "UnsupportedRecordTypeError",
    "append_mapped",
    "append_native",
    "map_record",
    "mapped_kind",
    "notice_source",
]


class UnsupportedRecordTypeError(ValueError):
    """Raised when a v4 record type has no native event kind."""

    def __init__(self, record_type: str) -> None:
        self.record_type = record_type
        supported = ", ".join(sorted(RECORD_KIND_MAP))
        super().__init__(
            f"native host does not accept record type {record_type!r}; supported types: {supported}"
        )


@dataclass(frozen=True, slots=True)
class RecordMapping:
    """How one legacy record type becomes a native event."""

    kind: EventKind
    note: str
    normalise: Callable[[Any], dict[str, Any]] | None = None


def _normalise_title(data: Any, /) -> dict[str, Any]:
    """Normalise a session title record to the schema's title fact.

    The v4 record carried either a source string or a source mapping whose kind
    is "llm"; only an llm-sourced title may claim title_source="llm".
    """
    title = data.get("title", "") if isinstance(data, Mapping) else ""
    source = data.get("source") if isinstance(data, Mapping) else None
    kind = source.get("kind") if isinstance(source, Mapping) else source
    return {"title": str(title), "source": "llm" if kind == "llm" else "user"}


RECORD_KIND_MAP: types.MappingProxyType[str, RecordMapping] = types.MappingProxyType(
    {
        "agent-preset/selected": RecordMapping(kind=EventKind.PRESET_SELECTED, note="agent preset"),
        "model/selection": RecordMapping(kind=EventKind.MODEL_SELECTED, note="model selection"),
        "sandbox/mode": RecordMapping(kind=EventKind.SANDBOX_MODE, note="sandbox mode"),
        "approval/policy": RecordMapping(kind=EventKind.PERMISSION_POLICY, note="approval policy"),
        "working-directory/change": RecordMapping(
            kind=EventKind.WORKSPACE_CWD_CHANGED, note="working directory"
        ),
        "session/title": RecordMapping(
            kind=EventKind.SESSION_TITLE, note="session title", normalise=_normalise_title
        ),
        "todo/write": RecordMapping(kind=EventKind.TODO_WRITTEN, note="todo write"),
    }
)


def notice_source(source: Mapping[str, Any] | None, /) -> MessageSource:
    """Build the message source for an injected notice.

    The host's notice queue records where a notice came from; a mapping with a
    non-empty plugin or kind becomes a PluginSource, anything else is a user
    notice (the loop's own injected prompts are user messages).
    """
    if source:
        plugin = source.get("plugin") or source.get("kind")
        if isinstance(plugin, str) and plugin:
            return PluginSource(plugin=plugin)
    return UserSource()


def mapped_kind(record_type: str) -> EventKind:
    """Return the native event kind for a legacy record type, or raise."""
    entry = RECORD_KIND_MAP.get(record_type)
    if entry is None:
        raise UnsupportedRecordTypeError(record_type)
    return entry.kind


def map_record(
    record_type: str,
    data: Mapping[str, Any] | None = None,
) -> tuple[EventKind, dict[str, Any]]:
    """Resolve a legacy record type to its event kind and normalised payload.

    Raises UnsupportedRecordTypeError before the caller can append anything.
    """
    entry = RECORD_KIND_MAP.get(record_type)
    if entry is None:
        raise UnsupportedRecordTypeError(record_type)
    payload: dict[str, Any] = dict(data or {})
    if entry.normalise is not None:
        payload = entry.normalise(payload)
    return entry.kind, payload


def append_native(
    session: NativeSession,
    kind: EventKind | str,
    data: Mapping[str, Any] | None = None,
    **envelope: Any,
) -> Event:
    """Append one schema event to a native session.

    kind must name a schema event; data becomes the event payload and the
    keyword envelope (run_id, turn_id, step_id, trace_id, ts) is forwarded to
    NativeSession.append unchanged.
    """
    event_kind = kind if isinstance(kind, EventKind) else EventKind(kind)
    return session.append(event_kind, dict(data or {}), **envelope)


def append_mapped(
    session: NativeSession,
    record_type: str,
    data: Mapping[str, Any] | None = None,
    **envelope: Any,
) -> Event:
    """Append one legacy record type as its native event.

    The record type is resolved and the payload normalised before the session is
    touched, so an unsupported type raises without writing an event.
    """
    kind, payload = map_record(record_type, data)
    return session.append(kind, payload, **envelope)
