"""The session-query service: read-only access to durable session logs.

The plugin exposes :class:`deeppyro.session_query.SessionQuery` as the
`session_query` service.  It depends on the `sessions` service for the store
root, so mounting it never opens a second, divergent store.

Configuration (all optional, DSH-compatible names)::

    [session_query]
    readWindowMax = 50

The global section also accepts the camelCase spelling `[sessionQuery]`;
a direct plugin-row config wins only when no global section is present.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from deeppyro.api import Service
from deeppyro.core.errors import ConfigError
from deeppyro.session_query import EventFilter, SessionQuery

DEFAULT_READ_WINDOW_MAX = 50


class SessionQueryConfig(BaseModel):
    """Per-plugin configuration mirroring DSH's session query options."""

    readWindowMax: int = Field(default=DEFAULT_READ_WINDOW_MAX, ge=1)


class SessionQueryPlugin(Service):
    """Read-only session query service built on the sessions store."""

    provide = "session_query"
    inject: ClassVar[list[str]] = ["sessions"]
    plugin_id = "session-query"
    version = "0.1.0"
    description = "read-only session log queries"
    Config = SessionQueryConfig

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        sessions = ctx.service("sessions")
        store = getattr(sessions, "store", sessions)
        read_window = _resolve_read_window(config, _global_config(ctx))
        self.query = SessionQuery(store, read_window_max=read_window)

    # -- delegated read API ------------------------------------------------

    def list_summaries(self) -> list[Any]:
        return self.query.list_summaries()

    def get(self, session_id: str) -> Any:
        return self.query.get(session_id)

    def read_session(self, session_id: str) -> Any:
        return self.query.read_session(session_id)

    def read_surface(self, session_id: str) -> list[Any]:
        return self.query.read_surface(session_id)

    def list_events(self, session_id: str, **kwargs: Any) -> list[Any]:
        return self.query.list_events(session_id, **kwargs)

    def read_event(self, session_id: str, seq: int, **kwargs: Any) -> Any:
        return self.query.read_event(session_id, seq, **kwargs)

    def filter_events(self, session_id: str, event_filter: EventFilter) -> list[Any]:
        return self.query.filter_events(session_id, event_filter)

    def trace_session(self, session_id: str) -> Any:
        return self.query.trace_session(session_id)

    def trace_event(self, session_id: str, seq: int) -> list[Any]:
        return self.query.trace_event(session_id, seq)


def _global_config(ctx: Any) -> Any:
    try:
        return ctx.service("config").value
    except (AttributeError, KeyError):  # pragma: no cover - embedded contexts without config
        return None


def _resolve_read_window(config: Any, global_config: Any) -> int:
    """Resolve `readWindowMax` from the plugin row and global config section."""
    value: Any = None
    if isinstance(config, Mapping):
        value = config.get("readWindowMax")
    else:
        value = getattr(config, "readWindowMax", None)
    if global_config is not None:
        for name in ("session_query", "sessionQuery"):
            section = getattr(global_config, name, None)
            if isinstance(section, Mapping) and section.get("readWindowMax") is not None:
                value = section["readWindowMax"]
                break
            if isinstance(section, BaseModel):
                candidate = getattr(section, "readWindowMax", None)
                if candidate is not None:
                    value = candidate
                    break
    if value is None:
        value = DEFAULT_READ_WINDOW_MAX
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError("session_query.readWindowMax must be a positive integer")
    return value


__all__ = [
    "DEFAULT_READ_WINDOW_MAX",
    "SessionQueryConfig",
    "SessionQueryPlugin",
]
