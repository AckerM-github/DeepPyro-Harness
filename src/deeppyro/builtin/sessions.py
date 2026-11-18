"""The sessions service: the native session control plane.

One root directory holds the schema 1 session logs; :class:`NativeSessionControl`
reads and writes them for the TUI, the web host and the tools. Native methods
return native objects and native errors: NativeQueryError for unknown ids,
SessionLockedError when another writer holds a session open and NativeForkError
when a fork cannot be created.
"""

from __future__ import annotations

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

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.root = sessions_dir()
        self.native = NativeSessionControl(root=self.root)

    def native_create(self, spec: NewSession) -> NativeSession:
        """Create one schema 1 session and return it open (caller closes it).

        Raises FileExistsError when the target session id already exists.
        """
        return self.native.create(spec)

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
        """List readable sessions newest first, optionally including archived ones."""
        return self.native.list(include_archived=include_archived, limit=limit, offset=offset)

    def native_latest(self, *, include_archived: bool = False) -> NativeSessionInfo | None:
        """Return the newest readable session, or None when there is none."""
        return self.native.latest(include_archived=include_archived)

    def native_transcript(self, session_id: str) -> NativeTranscript:
        """Fold one session into bounded display entries for the TUI."""
        return self.native.transcript(session_id)

    def native_rename(self, session_id: str, title: str, *, source: str = "user") -> str:
        """Append a session.title fact and return the normalised title."""
        return self.native.rename(session_id, title, source=source)

    def native_archive(self, session_id: str, *, archived: bool = True) -> bool:
        """Append an archive marker and return the resulting archived state."""
        return self.native.archive(session_id, archived=archived)

    def native_fork(self, session_id: str, *, new_id: str | None = None) -> NativeSession:
        """Copy a session into a fork and return it open (caller closes it)."""
        return self.native.fork(session_id, new_id=new_id)
