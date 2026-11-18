"""Kernel plugin face of the web layer.

WebHostPlugin provides the web service (provide="web"). It owns the route
registry so other plugins can contribute a route or a UI surface without
editing web/server.py, and it never opens a socket during setup: the bundle
host or the TUI splash calls start() explicitly. That keeps a mounted web
plugin inert in headless runs and lets the single executable boot the same
kernel graph later.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel, Field

from deeppyro.api.plugin import Service
from deeppyro.web.host import WebHost
from deeppyro.web.security import LOCALHOST, new_token
from deeppyro.web.service import resolve_plugin_services
from deeppyro.web.surface import RouteRegistry, WebSurface, default_surfaces
from deeppyro.web.version import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deeppyro.api.context import PluginContext
    from deeppyro.api.types import PluginConfig

logger = logging.getLogger("deeppyro.web.plugin")


class WebHostConfig(BaseModel):
    """Bundle-provided defaults for the loopback web host.

    autostart is deliberately not honored by setup(): starting a listener is
    always an explicit start() call from the host that owns the process.
    """

    host: str = LOCALHOST
    port: int = Field(default=0, ge=0, le=65535)
    cwd: str | None = None
    autostart: bool = False
    session: str | None = None
    provider: str | None = None
    model: str | None = None
    token: str | None = None


class WebHostPlugin(Service):
    """The web service: a route registry plus a lazily started WebHost."""

    plugin_id = "web"
    version = __version__
    description = "Host-only loopback web UI for the native agent runtime"
    provide = "web"
    Config = WebHostConfig
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: PluginContext, config: PluginConfig = None) -> None:
        super().__init__(ctx, config)
        settings = self.config if isinstance(self.config, WebHostConfig) else WebHostConfig()
        self.settings = settings
        configured = (settings.token or "").strip()
        self._token = configured if configured else new_token()
        self._registry = RouteRegistry()
        for surface in default_surfaces(lambda: self._token):
            self._registry.add_surface(surface)
        self._host: WebHost | None = None
        self._start_lock = asyncio.Lock()

    def setup(self) -> None:
        """Register nothing but the provided service; never listen here."""
        logger.debug("web host plugin mounted; call start() to bind the loopback port")

    # -- extension seam ----------------------------------------------------

    @property
    def token(self) -> str:
        """The bearer token the started host and the template share."""
        return self._token

    @property
    def registry(self) -> RouteRegistry:
        """The route registry plugins may contribute surfaces to."""
        return self._registry

    def register_surface(self, surface: WebSurface) -> None:
        """Add a plugin surface (a group of routes) to the host."""
        self._registry.add_surface(surface)

    def register_routes(self, surface: WebSurface, *extra: WebSurface) -> None:
        """Register one or more surfaces (convenience for plugin setup code)."""
        for item in (surface, *extra):
            self._registry.add_surface(item)

    # -- lifecycle ---------------------------------------------------------

    @property
    def host(self) -> WebHost | None:
        """The running host, or None before start()."""
        return self._host

    @property
    def url(self) -> str | None:
        """Tokenized URL of the running host, or None before start()."""
        return None if self._host is None else self._host.url

    async def start(
        self,
        *,
        port: int | None = None,
        cwd: str | Path | None = None,
        session_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> WebHost:
        """Start listening (explicitly asked) and return the running host."""
        async with self._start_lock:
            if self._host is not None:
                return self._host
            services = resolve_plugin_services(self.ctx)
            settings = self.settings
            host = await WebHost.start(
                services=services,
                port=settings.port if port is None else port,
                cwd=cwd if cwd is not None else settings.cwd,
                session_id=session_id if session_id is not None else settings.session,
                provider=provider if provider is not None else settings.provider,
                model=model if model is not None else settings.model,
                host=settings.host,
                token=self._token,
                registry=self._registry,
            )
            self._host = host
            logger.info("web host listening at %s", host.url)
            return host

    async def stop(self) -> None:
        """Stop the running host, if any (also called on plugin unmount)."""
        async with self._start_lock:
            host = self._host
            self._host = None
        if host is not None:
            await host.stop()

    def __repr__(self) -> str:
        state = "running" if self._host is not None else "mounted"
        return f"<WebHostPlugin {state} token-set={bool(self._token)}>"


def surfaces_of(*items: WebSurface) -> tuple[WebSurface, ...]:
    """Small helper so callers can build a surface tuple fluently."""
    return tuple(item for item in items if isinstance(item, WebSurface))


__all__ = ["WebHostConfig", "WebHostPlugin", "surfaces_of"]
