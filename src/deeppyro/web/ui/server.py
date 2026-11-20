"""ASGI server for the static React/Vite web UI.

The server owns the loopback socket, the cookie/token middleware and the JSON
API. The browser surface is a prebuilt single-page app: `static/index.html` is
the shell, `static/assets` carries the hashed Vite bundle and `assets` keeps
the shared brand files. Every non-API path falls back to the shell so browser
routing works, while `/api/*` stays a pure JSON surface.

Agent state stays behind WebSessionHost; the JSON routes in api.py are pure
projections and remain available for scripts and tests. The public surface
(`address`, `port`, `url`, `closing`, `start`, `serve_forever`,
`close`) deliberately matches the original stdlib WebServer so WebHost can
choose either server without changing its lifecycle.
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
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from deeppyro.web.security import LOCALHOST, SECURITY_HEADERS, validate_bind_host
from deeppyro.web.ui.api import UiContext, build_router
from deeppyro.web.ui.auth import UI_CSP, LoopbackAuthMiddleware
from deeppyro.web.wire import HttpError

#: Where the shared brand assets live next to this module (/brand).
ASSET_DIR = Path(__file__).with_name("assets")
#: Where the Vite build (index.html plus assets/) is written next to this module.
STATIC_DIR = Path(__file__).with_name("static")
#: Body of the 503 returned while the SPA bundle has not been built.
MISSING_BUILD_MESSAGE = "Web UI is not built. Run pnpm build in web/."
#: Cache policy for Vite's content-hashed bundle under /assets: the filename
#: changes with the content, so a client may reuse a cached copy indefinitely.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
#: Cache policy for unhashed replies: the SPA shell and the shared brand assets
#: must be revalidated on every load so a rebuilt bundle is picked up at once.
NO_STORE_CACHE_CONTROL = "no-store"


@dataclass
class _PortRef:
    """Mutable port the auth middleware reads after the socket is bound."""

    value: int = 0


class CacheControlledStaticFiles(StaticFiles):
    """Static files that stamp one explicit Cache-Control policy on every reply."""

    cache_control: str

    def __init__(self, *, directory: Path, cache_control: str) -> None:
        self.cache_control = cache_control
        super().__init__(directory=directory)

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        response.headers["cache-control"] = self.cache_control
        return response


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


def _index_response() -> FileResponse | PlainTextResponse:
    """Return the SPA shell, or a 503 while the Vite build is missing.

    The shell is unhashed, so it must not be cached: a stale shell would keep
    pointing browsers at an old bundle after every rebuild.
    """
    index = STATIC_DIR / "index.html"
    no_store = {"cache-control": NO_STORE_CACHE_CONTROL}
    if not index.is_file():
        return PlainTextResponse(MISSING_BUILD_MESSAGE, status_code=503, headers=no_store)
    return FileResponse(index, headers=no_store)


def _is_api_path(full_path: str) -> bool:
    """Whether a catch-all path belongs to the JSON API."""
    return full_path == "api" or full_path.startswith("api/")


def create_app(
    *,
    service_provider: Callable[[], Any],
    context: UiContext,
    port: _PortRef,
    bind_host: str = LOCALHOST,
) -> FastAPI:
    """Compose the SPA shell, the shared assets and the JSON API into one ASGI app."""
    app = FastAPI(
        title="DeepPyro",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        default_response_class=JSONResponse,
    )
    app.include_router(build_router(service_provider=service_provider, context=context))
    # Brand assets and the SPA shell share human-readable, unhashed filenames:
    # no-store, while the content-hashed Vite bundle is immutable.
    app.mount(
        "/brand",
        CacheControlledStaticFiles(directory=ASSET_DIR, cache_control=NO_STORE_CACHE_CONTROL),
        name="brand",
    )
    assets_dir = STATIC_DIR / "assets"
    if assets_dir.is_dir():
        app.mount(
            "/assets",
            CacheControlledStaticFiles(directory=assets_dir, cache_control=IMMUTABLE_CACHE_CONTROL),
            name="assets",
        )

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

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return _index_response()

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str) -> Response:
        if _is_api_path(full_path):
            # The JSON API is the only surface exempt from the SPA fallback;
            # an unknown API path must stay a JSON 404, never an HTML shell.
            raise StarletteHTTPException(status_code=404)
        return _index_response()

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
    """A loopback uvicorn server bound to the SPA ASGI app."""

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
                lifespan="on",
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
    "ASSET_DIR",
    "IMMUTABLE_CACHE_CONTROL",
    "NO_STORE_CACHE_CONTROL",
    "STATIC_DIR",
    "CacheControlledStaticFiles",
    "SecurityHeadersMiddleware",
    "UiServer",
    "create_app",
]
