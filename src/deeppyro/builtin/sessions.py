"""The sessions service: durable session.v4 store."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Service
from deeppyro.config import session_dir
from deeppyro.session import SessionStore


class SessionsPlugin(Service):
    """Exposes the SessionStore and convenience constructors."""

    provide = "sessions"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "sessions"
    version = "0.1.0"
    description = "durable session log store"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.service("config").value
        self.store = SessionStore(root=session_dir(cfg))

    def create(self, cwd: str, **kwargs: Any) -> Any:
        return self.store.create(cwd, **kwargs)

    def open(self, session_id: str) -> Any:
        return self.store.open(session_id)

    def list(self) -> list[Any]:
        return self.store.list()

    def latest(self) -> Any:
        return self.store.latest()
