"""Per-session committed working directory.

The service owns the model-visible execution directory.  It is deliberately
small: the agent session reads the current value for every tool context and
runtime-context projection, while this service records changes as durable
working-directory/change events and queues the DSH notice message.

The host can either configure one set of default callbacks:

    service.configure(
        default_directory=workspace,
        record=session.append,
        inject_notice=lambda text: session.inject(text, source={"kind": "working-directory"}),
    )

or bind callbacks per session (preferred when one runtime hosts several
sessions):

    service.bind(session, cwd=header.cwd, record=session.append,
                 inject_notice=lambda text: session.inject(text, source=...))
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from deeppyro.core.errors import AbortedError, ConfigError, ToolError

from .types import NoticeFn, RecordFn, WorkingDirectoryState

logger = logging.getLogger(__name__)

_CANCEL_MESSAGE = "working-directory operation was cancelled"


def _config_value(config: Any, *keys: str) -> Any:
    """Read the first present config key from a dict or arbitrary object."""
    for key in keys:
        if isinstance(config, Mapping):
            if key in config:
                return config[key]
        elif config is not None and hasattr(config, key):
            value = getattr(config, key)
            if value is not None:
                return value
    return None


def _quote(path: Path) -> str:
    """JSON-quote a path the same way DSH does in notices."""
    return json.dumps(str(path), ensure_ascii=False)


def _session_id(session: Any) -> str:
    if isinstance(session, str):
        if not session:
            raise ToolError("working-directory: session id must not be empty")
        return session
    if isinstance(session, Mapping):
        value = session.get("session_id") or session.get("sessionId")
    else:
        value = getattr(session, "session_id", None)
        if value is None:
            value = getattr(session, "sessionId", None)
    if not value:
        raise ToolError("working-directory: session has no session_id")
    return str(value)


def _header_cwd(session: Any) -> Path | None:
    if isinstance(session, (str, Mapping)):
        return None
    header = getattr(session, "header", None)
    if header is None:
        return None
    value = getattr(header, "cwd", None)
    if value is None:
        return None
    return Path(str(value)).expanduser()


def _normalise_path(value: str | Path, *, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


class WorkingDirectoryService:
    """Committed working-directory state and change operations.

    The class does not inherit cordis Service: the kernel plugin owns
    registration (see deeppyro.builtin.working_directory), matching the jobs
    service pattern.  Keeping the service independent also makes it directly
    testable without a plugin context.
    """

    provide: ClassVar[str] = "working_directory"
    inject: ClassVar[list[str]] = ["sessions"]

    def __init__(self, config: Any = None) -> None:
        raw_default = _config_value(config, "defaultDirectory", "default_directory")
        self._default_directory = self._parse_default(raw_default)
        self._global_record: RecordFn | None = None
        self._global_notice: NoticeFn | None = None
        self._states: dict[str, WorkingDirectoryState] = {}
        self._records: dict[str, RecordFn] = {}
        self._notices: dict[str, NoticeFn] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _parse_default(value: Any) -> Path:
        if value is None:
            return Path.cwd().resolve()
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ConfigError("working-directory: defaultDirectory must be absolute")
        return path.resolve(strict=False)

    @property
    def default_directory(self) -> Path:
        """Fallback directory used when a session has no header cwd."""
        return self._default_directory

    def configure(
        self,
        *,
        default_directory: str | Path | None = None,
        record: RecordFn | None = None,
        inject_notice: NoticeFn | None = None,
    ) -> None:
        """Install host callbacks used when a session has no per-session binding."""
        if default_directory is not None:
            self._default_directory = self._parse_default(default_directory)
        if record is not None:
            self._global_record = record
        if inject_notice is not None:
            self._global_notice = inject_notice

    def bind(
        self,
        session: Any,
        *,
        cwd: str | Path | None = None,
        record: RecordFn | None = None,
        inject_notice: NoticeFn | None = None,
        recovered: bool = False,
    ) -> Path:
        """Bind one session to an initial cwd and per-session callbacks.

        This is an optional convenience for hosts that manage several sessions
        through one service instance.  A session object with a header.cwd is
        initialised automatically on first access, so bind() is only required
        when the host must supply a different cwd or callbacks.
        """
        session_id = _session_id(session)
        if cwd is None:
            existing = self._states.get(session_id)
            if existing is not None:
                target = existing.cwd
            else:
                target = _header_cwd(session)
                if target is None or not target.is_absolute():
                    target = _normalise_path(target or self._default_directory, base=self._default_directory)
        else:
            target = _normalise_path(cwd, base=self._default_directory)
        self._states[session_id] = WorkingDirectoryState(cwd=target, recovered=recovered)
        if record is not None:
            self._records[session_id] = record
        if inject_notice is not None:
            self._notices[session_id] = inject_notice
        return target

    def unbind(self, session: Any) -> None:
        """Forget all state and callbacks for one session."""
        session_id = _session_id(session)
        self._states.pop(session_id, None)
        self._records.pop(session_id, None)
        self._notices.pop(session_id, None)
        self._locks.pop(session_id, None)

    def get(self, session: Any) -> Path:
        """Return the committed cwd without filesystem I/O."""
        return self._state(session).cwd

    async def ensure(self, session: Any, *, cancel: asyncio.Event | None = None) -> Path:
        """Validate that the current cwd still exists.

        P1 deliberately does not implement DSH's automatic recovery to the
        session header cwd; the caller gets an error and can cd explicitly.
        """
        session_id = _session_id(session)
        async with self._lock_for(session_id):
            self._check_cancel(cancel)
            current = self.get(session)
            if current.is_dir():
                return current
            raise ToolError(f"working-directory: current directory is unavailable: {_quote(current)}")

    async def set(
        self,
        session: Any,
        path: str | Path,
        *,
        cancel: asyncio.Event | None = None,
    ) -> Path:
        """Resolve, validate and commit a new working directory."""
        if not isinstance(path, (str, Path)):
            raise ToolError("working-directory: cd must be a string")
        raw = str(path)
        if not raw.strip():
            raise ToolError("working-directory: cd must not be empty")
        session_id = _session_id(session)
        async with self._lock_for(session_id):
            self._check_cancel(cancel)
            current = self.get(session)
            target = _normalise_path(raw, base=current)
            if not target.is_dir():
                raise ToolError(f"working-directory: directory does not exist: {_quote(target)}")
            if target == current:
                return current
            record = self._record_for(session_id)
            if record is None:
                raise ToolError("working-directory: the service is not configured for this session")
            record("working-directory/change", {"cwd": str(target)})
            self._states[session_id] = WorkingDirectoryState(cwd=target)
            notice = self._notice_for(session_id)
            if notice is not None:
                text = self._notice_text(current, target)
                try:
                    notice(text)
                except Exception:  # pragma: no cover - defensive; exercise in tests
                    logger.warning("failed to inject working-directory notice", exc_info=True)
            return target

    def context_text(self, session: Any) -> str:
        """Render the runtime-context line for the current session cwd."""
        return f"Current working directory: {_quote(self.get(session))}."

    # -- internals -----------------------------------------------------
    def _state(self, session: Any) -> WorkingDirectoryState:
        session_id = _session_id(session)
        state = self._states.get(session_id)
        if state is not None:
            return state
        header = _header_cwd(session)
        if header is None or not header.is_absolute():
            cwd = _normalise_path(header or self._default_directory, base=self._default_directory)
        else:
            cwd = header.resolve(strict=False)
        state = WorkingDirectoryState(cwd=cwd)
        self._states[session_id] = state
        return state

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _record_for(self, session_id: str) -> RecordFn | None:
        return self._records.get(session_id) or self._global_record

    def _notice_for(self, session_id: str) -> NoticeFn | None:
        return self._notices.get(session_id) or self._global_notice

    @staticmethod
    def _check_cancel(cancel: asyncio.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise AbortedError(_CANCEL_MESSAGE)

    @staticmethod
    def _notice_text(previous: Path, current: Path) -> str:
        return f"The working directory changed from {_quote(previous)} to {_quote(current)}."


__all__ = ["WorkingDirectoryService"]
