"""ASGI server for the P1 single-page web UI.

The server owns exactly three things: a loopback socket, the cookie/token
middleware, and the static shell. Agent state stays behind WebSessionHost; the
JSON routes in api.py are pure projections. Nothing here imports the TUI or any
removed compatibility layer.

The public surface (`address`, `port`, `url`, `closing`, `start`,
`serve_forever`, `close`) deliberately matches the original stdlib WebServer
so WebHost can choose either server without changing its lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from deeppyro.web.security import LOCALHOST, SECURITY_HEADERS, validate_bind_host
from deeppyro.web.ui.api import UiContext, build_router
from deeppyro.web.ui.auth import UI_CSP, LoopbackAuthMiddleware
from deeppyro.web.ui.shell import build_shell
from deeppyro.web.wire import HttpError

#: Where the browser assets live next to this module.
ASSET_DIR = Path(__file__).with_name("assets")

#: Asset name -> (filename, media type).
ASSETS: dict[str, tuple[str, str]] = {
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "cyborg-mark.svg": ("cyborg-mark.svg", "image/svg+xml"),
    "InterVariable.woff2": ("InterVariable.woff2", "font/woff2"),
}


@dataclass
class _PortRef:
    """Mutable port the auth middleware reads after the socket is bound."""

    value: int = 0


class SecurityHeadersMiddleware:
    """Attach the loopback CSP and the shared hardening headers to every reply."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() != b"content-security-policy"
                ]
                present = {name.lower() for name, _ in headers}
                additions = [
                    (name.encode("latin-1"), value.encode("latin-1"))
                    for name, value in SECURITY_HEADERS
                    if name.lower().encode("latin-1") not in present
                    and name.lower() != "content-security-policy"
                ]
                headers.append((b"content-security-policy", UI_CSP.encode("latin-1")))
                message = {**message, "headers": [*headers, *additions]}
            await send(message)

        await self._app(scope, receive, send_with_headers)


def _http_error(exc: HttpError) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    headers = {"cache-control": "no-store"}
    if exc.allow:
        headers["allow"] = ", ".join(exc.allow)
        payload["error"]["allow"] = list(exc.allow)
    return JSONResponse(payload, status_code=exc.status, headers=headers)


def create_app(
    *,
    service_provider: Callable[[], Any],
    context: UiContext,
    port: _PortRef,
    bind_host: str = LOCALHOST,
) -> FastAPI:
    """Compose the shell, the assets and the JSON API into one ASGI app."""
    app = FastAPI(
        title="DeepPyro",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        default_response_class=JSONResponse,
    )
    app.include_router(build_router(service_provider=service_provider, context=context))

    @app.exception_handler(HttpError)
    async def http_error_handler(request: Request, exc: HttpError) -> JSONResponse:
        return _http_error(exc)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "method_not_allowed"
        return JSONResponse(
            {"error": {"code": code, "message": str(exc.detail)}},
            status_code=exc.status_code,
            headers={"cache-control": "no-store"},
        )

    def shell(path: str, *, section: str | None = None, session_id: str | None = None) -> str:
        return build_shell(
            path=path,
            section=section,
            session_id=session_id,
            version=context.version,
        )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(shell("/"))

    @app.get("/c/{session_id}", response_class=HTMLResponse, include_in_schema=False)
    async def session_page(session_id: str) -> HTMLResponse:
        return HTMLResponse(shell(f"/c/{session_id}", session_id=session_id))

    @app.get("/palette", response_class=HTMLResponse, include_in_schema=False)
    async def palette_page() -> HTMLResponse:
        return HTMLResponse(shell("/palette"))

    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    async def settings_page() -> HTMLResponse:
        return HTMLResponse(shell("/settings"))

    @app.get("/settings/{section}", response_class=HTMLResponse, include_in_schema=False)
    async def settings_section_page(section: str) -> HTMLResponse:
        return HTMLResponse(shell(f"/settings/{section}", section=section))

    @app.get("/ui/{asset}", include_in_schema=False)
    async def asset(asset: str) -> Response:
        entry = ASSETS.get(asset)
        if entry is None:
            return JSONResponse(
                {"error": {"code": "not_found", "message": "no such asset"}},
                status_code=404,
                headers={"cache-control": "no-store"},
            )
        filename, media_type = entry
        body = (ASSET_DIR / filename).read_bytes()
        return Response(
            content=body,
            media_type=media_type,
            headers={"cache-control": "public, max-age=3600"},
        )

    # Middleware order: security outermost, auth inside it, so redirects and
    # rejections carry the same hardening headers as normal pages.
    app.add_middleware(
        LoopbackAuthMiddleware,
        token=context.token,
        port=lambda: port.value,
        bind_host=bind_host,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app


class UiServer:
    """A loopback uvicorn server bound to the P1 ASGI app."""

    def __init__(
        self,
        *,
        token: str,
        service_provider: Callable[[], Any],
        context: UiContext,
        host: str = LOCALHOST,
        port: int = 0,
        idle_timeout: float = 30.0,
    ) -> None:
        self._address = validate_bind_host(host)
        self._requested_port = int(port)
        self._idle_timeout = float(idle_timeout)
        self._port_ref = _PortRef()
        self._app = create_app(
            service_provider=service_provider,
            context=context,
            port=self._port_ref,
            bind_host=self._address,
        )
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._socket: socket.socket | None = None
        self._closing = False

    @property
    def address(self) -> tuple[str, int]:
        """The bound (address, port) pair; port is 0 before start."""
        return (self._address, self._port_ref.value)

    @property
    def port(self) -> int:
        """The bound TCP port after start."""
        return self._port_ref.value

    @property
    def url(self) -> str:
        """The base URL of the bound server (no token)."""
        return f"http://{self._address}:{self._port_ref.value}/"

    @property
    def closing(self) -> bool:
        """Whether the server is shutting down."""
        return self._closing

    @property
    def app(self) -> FastAPI:
        """The ASGI app, for tests that call it directly."""
        return self._app

    async def start(self) -> int:
        """Bind the loopback socket and start uvicorn on it."""
        if self._server is not None:
            raise RuntimeError("web server already started")
        family = socket.AF_INET6 if ":" in self._address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self._address, self._requested_port))
            sock.listen(64)
            sock.setblocking(False)
            self._socket = sock
            self._port_ref.value = int(sock.getsockname()[1])
            config = uvicorn.Config(
                self._app,
                log_level="warning",
                access_log=False,
                lifespan="off",
                timeout_keep_alive=int(self._idle_timeout),
            )
            server = uvicorn.Server(config)
            self._server = server
            self._closing = False
            self._task = asyncio.create_task(server.serve(sockets=[sock]), name="dpyro-web-ui")
            for _ in range(500):
                if server.started:
                    break
                if self._task.done():
                    error = self._task.exception()
                    raise RuntimeError(f"web server failed to start: {error}")
                await asyncio.sleep(0.01)
            else:  # pragma: no cover - startup is local and immediate
                raise RuntimeError("web server did not start in time")
        except BaseException:
            self._closing = True
            if self._task is not None:
                self._task.cancel()
                with contextlib.suppress(Exception):
                    await self._task
                self._task = None
            self._server = None
            with contextlib.suppress(Exception):
                sock.close()
            self._socket = None
            raise
        return self._port_ref.value

    async def serve_forever(self) -> None:
        """Serve until close() is called."""
        if self._server is None:
            await self.start()
        task = self._task
        if task is None:  # pragma: no cover - start() raises instead
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def close(self) -> None:
        """Stop accepting, then drain in-flight requests."""
        if self._closing:
            return
        self._closing = True
        server, self._server = self._server, None
        task, self._task = self._task, None
        sock, self._socket = self._socket, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
                except TimeoutError:
                    task.cancel()
                    with contextlib.suppress(Exception):
                        await task
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.close()


__all__ = [
    "ASSETS",
    "ASSET_DIR",
    "SecurityHeadersMiddleware",
    "UiServer",
    "create_app",
]
