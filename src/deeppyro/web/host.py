"""Process entry point for the host-only web UI.

WebHost is the one-call seam the TUI splash will use later: await
WebHost.start(...) boots (or adopts) the plugin kernel, starts the loopback
HTTP host on an ephemeral port, attaches a session and returns the URL with
the per-process bearer token. It is also what python -m deeppyro.web runs.

Nothing here is a second agent implementation: the session host in service.py
streams the same NativeAgentHost that the TUI uses.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO, cast
from urllib.parse import quote

from deeppyro.config import Config, load_config
from deeppyro.kernel import boot
from deeppyro.web.assets import TITLE
from deeppyro.web.security import LOCALHOST, new_token, validate_bind_host
from deeppyro.web.server import WebServer
from deeppyro.web.service import (
    HostServices,
    WebSessionHost,
    resolve_host_services,
    resolve_workspace,
)
from deeppyro.web.surface import RouteRegistry, WebSurface, default_surfaces
from deeppyro.web.version import __version__
from deeppyro.web.wire import CreateSessionRequest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from deeppyro.kernel.app import App

logger = logging.getLogger("deeppyro.web.host")


class WebHost:
    """A running loopback web host plus the session service behind it."""

    def __init__(
        self,
        *,
        token: str,
        service: WebSessionHost,
        server: WebServer,
        version: str,
        kernel_app: App | None = None,
        owns_kernel: bool = False,
        initial_session: Mapping[str, Any] | None = None,
    ) -> None:
        self._token = token
        self._service = service
        self._server = server
        self._version = version
        self._kernel_app = kernel_app
        self._owns_kernel = owns_kernel
        self._initial_session = initial_session
        self._stopped = False

    # -- construction ------------------------------------------------------

    @classmethod
    async def start(
        cls,
        *,
        kernel_app: App | None = None,
        services: HostServices | None = None,
        config: Config | None = None,
        port: int = 0,
        cwd: str | Path | None = None,
        session_id: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        host: str = LOCALHOST,
        token: str | None = None,
        surfaces: Iterable[WebSurface] = (),
        registry: RouteRegistry | None = None,
        version: str = __version__,
    ) -> WebHost:
        """Start the loopback host, attach a session and return it.

        The caller owns the returned host and must await stop(). When both
        kernel_app and services are None a fresh kernel is booted from config
        (loaded from the deeppyro home when config is None) and stopped again on
        stop(). When kernel_app is supplied it is adopted, never stopped.
        """
        if port < 0 or port > 65535:
            raise ValueError(f"invalid port: {port}")
        bind = validate_bind_host(host)
        workspace = resolve_workspace(cwd if cwd is not None else Path.cwd())
        token = token if token else new_token()

        app = kernel_app
        owns_kernel = False
        service: WebSessionHost | None = None
        server: WebServer | None = None
        try:
            if app is None and services is None:
                app = await boot(config if config is not None else load_config())
                owns_kernel = True
            if services is None:
                if app is None:  # pragma: no cover - defensive
                    raise ValueError("a web host needs a kernel app or resolved services")
                services = resolve_host_services(app)
            service = WebSessionHost(services, default_cwd=workspace, version=version)
            await service.start()
            routes = registry if registry is not None else RouteRegistry()
            if registry is None:
                for surface in default_surfaces(lambda: token):
                    routes.add_surface(surface)
            for surface in surfaces:
                routes.add_surface(surface)
            server = WebServer(
                token=token,
                registry=routes,
                service_provider=lambda: service,
                host=bind,
                port=port,
            )
            await server.start()
            initial: Mapping[str, Any]
            if session_id:
                initial = await service.resume_session(session_id)
            else:
                initial = await service.create_session(
                    CreateSessionRequest(
                        cwd=str(workspace),
                        provider=provider,
                        model=model,
                    )
                )
        except BaseException:
            if server is not None:
                with contextlib.suppress(Exception):
                    await server.close()
            if service is not None:
                with contextlib.suppress(Exception):
                    await service.close()
            if owns_kernel and app is not None:
                with contextlib.suppress(Exception):
                    await app.stop()
            raise
        if server is None or service is None:  # pragma: no cover - defensive
            raise RuntimeError("web host start() finished without a server")
        return cls(
            token=token,
            service=cast(WebSessionHost, service),
            server=server,
            version=version,
            kernel_app=app,
            owns_kernel=owns_kernel,
            initial_session=initial,
        )

    # -- introspection -----------------------------------------------------

    @property
    def token(self) -> str:
        """The per-process bearer token protecting every API route."""
        return self._token

    @property
    def port(self) -> int:
        """The bound loopback port."""
        return self._server.port

    @property
    def bound_host(self) -> str:
        """The bound address, always a loopback name."""
        return self._server.address[0]

    @property
    def base_url(self) -> str:
        """Loopback base URL without the token."""
        return f"http://{self.bound_host}:{self.port}/"

    @property
    def url(self) -> str:
        """The URL to open, including the bearer token query parameter."""
        return f"{self.base_url}?token={quote(self._token, safe='')}"

    @property
    def service(self) -> WebSessionHost:
        """The session host behind the HTTP surface."""
        return self._service

    @property
    def server(self) -> WebServer:
        """The asyncio HTTP server."""
        return self._server

    @property
    def kernel_app(self) -> App | None:
        """The adopted or booted kernel app, when there is one."""
        return self._kernel_app

    @property
    def initial_session(self) -> Mapping[str, Any] | None:
        """The session attached at startup."""
        return self._initial_session

    # -- lifecycle ---------------------------------------------------------

    async def serve_forever(self) -> None:
        """Serve until stop() (or the process is interrupted)."""
        await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop listening, detach the session and stop an owned kernel."""
        if self._stopped:
            return
        self._stopped = True
        with contextlib.suppress(Exception):
            await self._server.close()
        with contextlib.suppress(Exception):
            await self._service.close()
        if self._owns_kernel and self._kernel_app is not None:
            with contextlib.suppress(Exception):
                await self._kernel_app.stop()

    def print_banner(self, stream: TextIO | None = None) -> None:
        """Print the startup banner with the tokenized loopback URL."""
        out = stream if stream is not None else sys.stdout
        print(f"{TITLE} {self._version}", file=out)
        print(f"  URL: {self.url}", file=out)
        print("  Localhost only: bound to 127.0.0.1, never exposed on the network.", file=out)
        if self._initial_session is not None:
            print(f"  Session: {self._initial_session.get('session_id', '')}", file=out)
        print("  Press Ctrl-C to stop.", file=out)
        out.flush()

    def __repr__(self) -> str:
        return f"<WebHost port={self.port} session={self.active_session_id!r}>"

    @property
    def active_session_id(self) -> str | None:
        """The attached session id, if any."""
        return self._service.active_session_id


__all__ = ["WebHost"]
