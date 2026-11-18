"""Fact adapters for schema 1 sessions.

The v4 runtime reads permission, policy and history facts from a session's
records list: objects exposing a type and a data mapping. Native sessions store
schema 1 events instead. This module projects events into the record shape the
existing services already understand, so permission presets, sandbox policy,
approval policy and the history helpers work unchanged without a second session
format and without fabricating a v4 session header.

The adapter is read-mostly: appends delegate to the native handle so supported
v4 record types keep landing as schema 1 events.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

#: Schema 1 event type mapped to the v4 record type used by fact readers.
EVENT_RECORD_TYPES: dict[str, str] = {
    "turn.started": "turn/start",
    "turn.ended": "turn/end",
    "step.started": "step/start",
    "step.ended": "step/end",
    "user.message": "user/message",
    "assistant.message": "assistant/message",
    "tool.call": "tool/call",
    "tool.result": "tool/result",
    "model.selected": "model/selection",
    "preset.selected": "agent-preset/selected",
    "sandbox.mode": "sandbox/mode",
    "permission.policy": "approval/policy",
    "workspace.cwd_changed": "working-directory/change",
    "session.title": "session/title",
}


def record_views(events: Any) -> list[Any]:
    """Project native events into v4-shaped record views (type plus data)."""
    views: list[Any] = []
    for event in events or ():
        record_type = EVENT_RECORD_TYPES.get(str(getattr(event, "type", "") or ""))
        if record_type is None:
            continue
        data = getattr(event, "data", None)
        views.append(SimpleNamespace(type=record_type, data=data if isinstance(data, dict) else {}))
    return views


class NativeRecordView:
    """Read-only v4-shaped session view over one native session handle.

    Services that scan session records (permission presets, sandbox policy,
    approval policy) and the history helpers accept this view. Appends delegate
    to the handle; the synthetic permission/preset record is dropped because
    schema 1 derives the preset from the sandbox mode and approval policy facts
    it already stores.
    """

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    @property
    def handle(self) -> Any:
        """The wrapped native handle."""
        return self._handle

    @property
    def native_session(self) -> Any:
        """The underlying schema 1 session."""
        return getattr(self._handle, "native_session", None)

    @property
    def header(self) -> Any:
        """The native header view."""
        return getattr(self._handle, "header", None)

    @property
    def session_id(self) -> str:
        """Stable session id."""
        return str(getattr(self._handle, "session_id", "") or "")

    @property
    def events(self) -> Any:
        """The underlying event tuple."""
        return tuple(getattr(self._handle, "events", ()) or ())

    @property
    def records(self) -> list[Any]:
        """V4-shaped records in log order (a fresh list on every read)."""
        return record_views(self.events)

    def append(self, record_type: str, data: Any = None) -> int:
        """Append one v4 record type through the native mapping when possible."""
        if record_type == "permission/preset":
            # Schema 1 stores the sandbox mode and approval policy instead; the
            # named preset is derived from those facts on every read.
            return -1
        return int(self._handle.append(record_type, data))

    def fold(self) -> Any:
        """Fold the session log."""
        return self._handle.fold()

    def derive_messages(self, **kwargs: Any) -> Any:
        """Conversation messages derived from the fold."""
        return self._handle.derive_messages(**kwargs)

    def usage(self) -> dict[str, int]:
        """Token usage counters for the session."""
        return self._handle.usage()

    def context_tokens(self) -> int:
        """Current context size in tokens."""
        return int(self._handle.context_tokens())

    def inject(self, text: str, source: Any = None) -> bool:
        """Queue a pending notice on the handle."""
        return bool(self._handle.inject(text, source=source))

    def __getattr__(self, name: str) -> Any:
        # Everything else is a straight read of the handle; never a write.
        return getattr(self._handle, name)


def native_view(session: Any) -> Any:
    """Wrap a native session or handle in the record view; v4 sessions pass through."""
    if session is None:
        return None
    if isinstance(session, NativeRecordView):
        return session
    if getattr(session, "records", None) is not None:
        return session
    if getattr(session, "events", None) is None:
        handle = getattr(session, "handle", None)
        if handle is None:
            return session
        session = handle
    return NativeRecordView(session)


__all__ = ["EVENT_RECORD_TYPES", "NativeRecordView", "native_view", "record_views"]
