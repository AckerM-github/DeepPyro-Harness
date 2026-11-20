"""The sessions service: the native session control plane.

One root directory holds the schema 1 session logs; :class:`NativeSessionControl`
reads and writes them for the TUI, the web host and the tools. Native methods
return native objects and native errors: NativeQueryError for unknown ids,
SessionLockedError when another writer holds a session open and NativeForkError
when a fork cannot be created.
"""

from __future__ import annotations

import threading
import time
from typing import ClassVar

from deeppyro.api import PluginConfig, PluginContext, Service
from deeppyro.paths import sessions_dir
from deeppyro.session.native_control import NativeSessionControl, NewSession
from deeppyro.session.native_index import NativeSessionInfo, NativeTranscript
from deeppyro.session.schema import NativeSession, SessionLog


class SessionsPlugin(Service):
    """Owns the session directory and the native control surface."""

    provide = "sessions"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "sessions"
    version = "0.3.0"
    description = "native session log store"

    #: How long one full directory scan may be reused. The settings screen and
    #: the session list ask for the same listing in a burst; a short TTL keeps
    #: that warm without hiding an external writer for long.
    list_cache_ttl_s: ClassVar[float] = 2.0

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.root = sessions_dir()
        self.native = NativeSessionControl(root=self.root)
        self._list_lock = threading.Lock()
        self._list_cache: dict[bool, tuple[float, list[NativeSessionInfo]]] = {}

    def native_create(self, spec: NewSession, *, deferred: bool = False) -> NativeSession:
        """Create one schema 1 session and return it open (caller closes it).

        deferred=True keeps the log in memory until the first user/turn event.
        Raises FileExistsError when the target session id already exists.
        """
        session = self.native.create(spec, deferred=deferred)
        self._invalidate_list_cache()
        return session

    def invalidate_list_cache(self) -> None:
        """Drop the short-lived catalogue cache after an out-of-band write.

        A deferred draft materializes its log during the first turn, outside
        native_create/native_archive, so the cached list would otherwise miss
        the just-written session until the TTL expires.
        """
        self._invalidate_list_cache()

    def native_open(self, session_id: str) -> NativeSession:
        """Open an existing session for writing; the caller owns the lock.

        Raises NativeQueryError for an unknown id and lets SessionLockedError
        through when another writer holds the session.
        """
        return self.native.open(session_id)

    def native_exists(self, session_id: str) -> bool:
        """Whether a native session with this id is readable."""
        return self.native.exists(session_id)

    def native_read(self, session_id: str) -> SessionLog:
        """Parse one session log without taking its write lock."""
        return self.native.read(session_id)

    def native_look(self, session_id: str) -> NativeSessionInfo:
        """Return one session's browser row (raises NativeQueryError when unknown)."""
        return self.native.look(session_id)

    def native_list(
        self,
        *,
        include_archived: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[NativeSessionInfo]:
        """List readable sessions newest first, optionally including archived ones.

        The full directory scan is cached for :attr:`list_cache_ttl_s` seconds
        and sliced after the fact, so repeated listings (the settings payload and
        the session sidebar ask at the same time) cost one scan per TTL window.
        Bad limit/offset values fail before the cache is consulted.
        """
        if type(limit) is not int and limit is not None:
            raise ValueError("limit must be an int or None")
        if type(offset) is not int:
            raise ValueError("offset must be an int")
        if offset < 0 or (limit is not None and limit < 0):
            raise ValueError("offset and limit must be non-negative")
        records = self._cached_records(include_archived=include_archived)
        end = None if limit is None else offset + limit
        return records[offset:end]

    def _cached_records(self, *, include_archived: bool) -> list[NativeSessionInfo]:
        """One directory scan per TTL window; only unchanged files are re-parsed."""
        now = time.monotonic()
        with self._list_lock:
            cached = self._list_cache.get(include_archived)
            if cached is not None and now - cached[0] <= self.list_cache_ttl_s:
                return cached[1]
        records = list(self.native.list(include_archived=include_archived))
        with self._list_lock:
            self._list_cache[include_archived] = (time.monotonic(), records)
        return records

    def _invalidate_list_cache(self) -> None:
        with self._list_lock:
            self._list_cache.clear()

    def native_latest(self, *, include_archived: bool = False) -> NativeSessionInfo | None:
        """Return the newest readable session, or None when there is none."""
        records = self._cached_records(include_archived=include_archived)
        return records[0] if records else None

    def native_transcript(self, session_id: str) -> NativeTranscript:
        """Fold one session into bounded display entries for the TUI."""
        return self.native.transcript(session_id)

    def native_rename(self, session_id: str, title: str, *, source: str = "user") -> str:
        """Append a session.title fact and return the normalised title."""
        title = self.native.rename(session_id, title, source=source)
        self._invalidate_list_cache()
        return title

    def native_archive(self, session_id: str, *, archived: bool = True) -> bool:
        """Append an archive marker and return the resulting archived state."""
        state = self.native.archive(session_id, archived=archived)
        self._invalidate_list_cache()
        return state

    def native_fork(self, session_id: str, *, new_id: str | None = None) -> NativeSession:
        """Copy a session into a fork and return it open (caller closes it)."""
        session = self.native.fork(session_id, new_id=new_id)
        self._invalidate_list_cache()
        return session
