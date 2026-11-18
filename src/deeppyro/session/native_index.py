"""Read-only index of schema 1 (native) session logs.

The TUI turn engine still drives v4 sessions; this module only *reads* native
schema 1 logs so the session browser can show sessions written by headless runs
and other native writers. Nothing here attaches to a session, mutates a log or
imports the wire layer: the TUI receives plain dataclasses.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.paths import sessions_dir
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import (
    Event,
    EventKind,
    NativeCodec,
    SessionHeader,
    SessionLog,
    sessions_path_for,
)

NATIVE_SOURCE = "native"
EMPTY_TITLE = "(untitled)"
TITLE_LIMIT = 24
PREVIEW_LIMIT = 80
ENTRY_TEXT_LIMIT = 4000
_COMMAND_RE = re.compile(r"^/[A-Za-z][A-Za-z0-9_-]+\s+")
_SENTENCE_ENDS = "。．！？!?\n\r"
_TRANSCRIPT_ROLES = frozenset({"user", "assistant", "tool"})
_USER_MESSAGE = EventKind.USER_MESSAGE.value
_TURN_STARTED = EventKind.TURN_STARTED.value
_SESSION_TITLE = EventKind.SESSION_TITLE.value


@dataclass(frozen=True, slots=True)
class NativeSessionInfo:
    """One schema 1 session as the read-only browser sees it."""

    id: str
    path: Path
    cwd: str
    created_at: int
    updated_at: int
    title: str
    turns: int
    event_count: int
    preset: str
    parent_id: str | None
    origin: str
    preview: str | None
    source: str = NATIVE_SOURCE
    readable: bool = True
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class NativeTranscriptEntry:
    """One ordered message in a native transcript, bounded for display."""

    role: str
    text: str
    tool_names: tuple[str, ...] = ()
    is_error: bool = False
    created_at: int = 0
    preview: str | None = None


@dataclass(frozen=True, slots=True)
class NativeTranscript:
    """A read-only transcript derived by folding one native session log."""

    header: SessionHeader
    info: NativeSessionInfo
    entries: tuple[NativeTranscriptEntry, ...] = ()
    issues: tuple[str, ...] = ()
    readable: bool = True

    @property
    def final_text(self) -> str | None:
        """The last assistant text in the transcript, if any."""
        for entry in reversed(self.entries):
            if entry.role == "assistant" and entry.text.strip():
                return entry.text
        return None


@dataclass(frozen=True, slots=True)
class _LoadedSession:
    log: SessionLog
    info: NativeSessionInfo
    view: SessionView | None
    error: str | None


def list_native_sessions(root: Path | None = None) -> list[NativeSessionInfo]:
    """List schema 1 sessions under *root* (default: the sessions dir), newest first.

    Files that are not native schema 1 logs, and logs that fail to parse, are
    skipped without raising: a session browser must never fail on one bad file.
    """
    base = Path(root) if root is not None else Path(sessions_dir())
    found: dict[str, NativeSessionInfo] = {}
    for path in _candidate_paths(base):
        info = _read_info(path)
        if info is None:
            continue
        current = found.get(info.id)
        if current is None or _precedence(info) > _precedence(current):
            found[info.id] = info
    return sorted(found.values(), key=lambda info: (info.created_at, info.id), reverse=True)


def read_native_transcript(session_id: str, root: Path | None = None) -> NativeTranscript:
    """Fold one native session into ordered user/assistant/tool entries.

    A log whose fold fails is returned with readable=False and the fold error
    in issues instead of raising; an unknown session id raises FileNotFoundError.
    """
    path = _resolve_path(session_id, root)
    loaded = _load_session(path)
    issues: list[str] = []
    if loaded.error is not None:
        issues.append(loaded.error)
    entries = _transcript_entries(loaded.view) if loaded.view is not None else ()
    return NativeTranscript(
        header=loaded.log.header,
        info=loaded.info,
        entries=entries,
        issues=tuple(issues),
        readable=loaded.error is None,
    )


# -- discovery -------------------------------------------------------------


def _candidate_paths(base: Path) -> list[Path]:
    """Flat and one/two-level nested schema 1 logs, deduplicated by real path."""
    if not base.is_dir():
        return []
    paths: list[Path] = []
    for pattern in ("*.jsonl", "*/*.jsonl", "*/*/*.jsonl"):
        try:
            paths.extend(base.glob(pattern))
        except OSError:
            continue
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _read_info(path: Path) -> NativeSessionInfo | None:
    try:
        return _load_session(path).info
    except Exception:  # noqa: BLE001 - foreign or malformed files are skipped by design
        return None


def _precedence(info: NativeSessionInfo) -> tuple[int, int, int]:
    return (1 if info.readable else 0, info.updated_at, info.event_count)


def _resolve_path(session_id: str, root: Path | None) -> Path:
    base = Path(root) if root is not None else Path(sessions_dir())
    direct = sessions_path_for(session_id, base)
    if direct.is_file():
        return direct
    for path in _candidate_paths(base):
        if path.stem == session_id:
            return path
    raise FileNotFoundError(f"native session not found: {session_id}")


# -- reading ---------------------------------------------------------------


def _load_session(path: Path) -> _LoadedSession:
    log = NativeCodec.read(path)
    view: SessionView | None = None
    error: str | None = None
    try:
        view = fold(log.header, log.events)
    except Exception as exc:  # noqa: BLE001 - a broken fold yields an unreadable row, not a crash
        error = f"{type(exc).__name__}: {exc}"
    info = _session_info(log, view, readable=error is None)
    return _LoadedSession(log=log, info=info, view=view, error=error)


def _session_info(log: SessionLog, view: SessionView | None, *, readable: bool) -> NativeSessionInfo:
    header = log.header
    user_text = _first_user_text(view, log.events)
    title = _title_from_view(view) or _last_title_event(log.events) or _derive_title(user_text) or EMPTY_TITLE
    return NativeSessionInfo(
        id=header.id,
        path=log.path,
        cwd=header.cwd,
        created_at=header.created_at,
        updated_at=log.events[-1].ts if log.events else header.created_at,
        title=title,
        turns=_turn_count(log.events),
        event_count=len(log.events),
        preset=header.preset,
        parent_id=header.parent_id,
        origin=header.origin,
        preview=_preview_text(user_text),
        readable=readable,
        read_only=True,
    )


def _title_from_view(view: SessionView | None) -> str | None:
    if view is None:
        return None
    title = view.state.title
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _last_title_event(events: tuple[Event, ...]) -> str | None:
    for event in reversed(events):
        if event.type != _SESSION_TITLE:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def _turn_count(events: tuple[Event, ...]) -> int:
    turns = sum(1 for event in events if event.type == _TURN_STARTED)
    if turns:
        return turns
    return sum(1 for event in events if event.type == _USER_MESSAGE)


def _first_user_text(view: SessionView | None, events: tuple[Event, ...]) -> str | None:
    if view is not None:
        for message in view.messages:
            if getattr(message, "role", "") != "user":
                continue
            text = _message_text(message)
            if text.strip():
                return text
    return _first_raw_user_text(events)


def _first_raw_user_text(events: Sequence[Event]) -> str | None:
    for event in events:
        if event.type != _USER_MESSAGE:
            continue
        data = event.data if isinstance(event.data, dict) else {}
        text = _message_text_from_raw(data.get("message"))
        if text.strip():
            return text
    return None


# -- transcript ------------------------------------------------------------


def _transcript_entries(view: SessionView) -> tuple[NativeTranscriptEntry, ...]:
    names = _tool_names_by_call_id(view)
    entries: list[NativeTranscriptEntry] = []
    for message in view.messages:
        role = str(getattr(message, "role", "") or "")
        if role not in _TRANSCRIPT_ROLES:
            continue
        text = _message_text(message)
        tool_names: tuple[str, ...] = ()
        if role == "assistant":
            tool_names = tuple(name for name in (_call_name(call) for call in _tool_calls(message)) if name)
        elif role == "tool":
            call_id = str(getattr(message, "tool_call_id", "") or "")
            resolved = names.get(call_id, "")
            tool_names = (resolved,) if resolved else ()
        if not text and not tool_names:
            continue
        entries.append(
            NativeTranscriptEntry(
                role=role,
                text=_bound_text(text),
                tool_names=tool_names,
                is_error=bool(getattr(message, "is_error", False)),
                created_at=int(getattr(message, "created_at", 0) or 0),
                preview=_preview_text(text),
            )
        )
    return tuple(entries)


def _tool_names_by_call_id(view: SessionView) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in view.messages:
        if getattr(message, "role", "") != "assistant":
            continue
        for call in _tool_calls(message):
            call_id = str(getattr(call, "id", "") or "")
            name = _call_name(call)
            if call_id and name:
                names[call_id] = name
    return names


def _tool_calls(message: Any) -> tuple[Any, ...]:
    getter = getattr(message, "tool_calls", None)
    if not callable(getter):
        return ()
    try:
        found = getter()
    except Exception:  # noqa: BLE001 - display helpers must not break the browser
        return ()
    if not isinstance(found, Iterable):
        return ()
    return tuple(found)


def _call_name(call: Any) -> str:
    return str(getattr(call, "name", "") or "")


def _message_text(message: Any) -> str:
    getter = getattr(message, "text", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception:  # noqa: BLE001 - display helpers must not break the browser
        return ""


def _message_text_from_raw(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""
    content = raw.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


# -- display helpers (mirrors of deeppyro.session.metadata, wire-free) ------


def _collapse(text: object, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "…"


def _strip_command(text: str) -> str:
    return _COMMAND_RE.sub("", text, count=1)


def _first_sentence(text: str) -> str:
    for index, char in enumerate(text):
        if char == ".":
            if index + 1 >= len(text) or text[index + 1].isspace():
                return text[:index]
            continue
        if char in _SENTENCE_ENDS:
            return text[:index]
    return text


def _derive_title(text: object) -> str | None:
    raw = "" if text is None else str(text)
    collapsed = _collapse(_first_sentence(_strip_command(raw)), TITLE_LIMIT)
    return collapsed or None


def _preview_text(text: object) -> str | None:
    return _collapse(text, PREVIEW_LIMIT) or None


def _bound_text(text: str) -> str:
    if len(text) <= ENTRY_TEXT_LIMIT:
        return text
    return text[: ENTRY_TEXT_LIMIT - 1].rstrip() + "…"


__all__ = [
    "EMPTY_TITLE",
    "NATIVE_SOURCE",
    "NativeSessionInfo",
    "NativeTranscript",
    "NativeTranscriptEntry",
    "list_native_sessions",
    "read_native_transcript",
]
