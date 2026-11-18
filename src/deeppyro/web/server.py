"""Stdlib-only asyncio HTTP/1.1 host for the loopback web UI.

No third-party server: one asyncio.start_server on 127.0.0.1, a keep-alive
HTTP/1.1 request loop, JSON commands, chunked SSE responses and static assets.
The token, Host/Origin validation and security headers are enforced here, in
front of every route handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, unquote

from deeppyro.web.events import HEARTBEAT_FRAME
from deeppyro.web.security import (
    LOCALHOST,
    SECURITY_HEADERS,
    require_token,
    validate_bind_host,
    validate_request_origin,
)
from deeppyro.web.surface import RouteRegistry
from deeppyro.web.wire import (
    MAX_BODY_BYTES,
    BadRequest,
    HttpError,
    Method,
    PayloadTooLarge,
    SSEStream,
    WebRequest,
    WebResponse,
    error_response,
)

logger = logging.getLogger("deeppyro.web.server")

#: Hard cap on the request line plus headers.
MAX_HEADER_BYTES = 64 * 1024

#: Maximum number of request headers.
MAX_HEADERS = 128

#: Seconds allowed between two reads on an idle keep-alive connection.
DEFAULT_IDLE_TIMEOUT = 30.0

#: Seconds allowed for one socket write to drain.
DEFAULT_WRITE_TIMEOUT = 30.0

_STATUS_PHRASES: dict[int, str] = {
    200: "OK",
    201: "Created",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


@dataclass(frozen=True, slots=True)
class IncomingRequest:
    """One parsed request plus the keep-alive decision for the connection."""

    request: WebRequest
    keep_alive: bool


def _status_line(status: int) -> bytes:
    phrase = _STATUS_PHRASES.get(status, "Unknown")
    return f"HTTP/1.1 {status} {phrase}\r\n".encode("ascii")


def _encode_headers(headers: list[tuple[str, str]]) -> bytes:
    return "".join(f"{name}: {value}\r\n" for name, value in headers).encode("latin-1") + b"\r\n"


def _dedupe_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for name, value in headers:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append((name, value))
    return unique


class WebServer:
    """A loopback-only HTTP/1.1 server bound to one route registry."""

    def __init__(
        self,
        *,
        token: str,
        registry: RouteRegistry,
        service_provider: Callable[[], object | None],
        host: str = LOCALHOST,
        port: int = 0,
        body_limit: int = MAX_BODY_BYTES,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
    ) -> None:
        self._token = token
        self._registry = registry
        self._service_provider = service_provider
        self._address = validate_bind_host(host)
        self._requested_port = int(port)
        self._body_limit = max(int(body_limit), 1)
        self._idle_timeout = float(idle_timeout)
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._closing = False
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def address(self) -> tuple[str, int]:
        """The bound (address, port) pair; port is 0 before start."""
        return (self._address, self._port)

    @property
    def port(self) -> int:
        """The bound TCP port after start."""
        return self._port

    @property
    def url(self) -> str:
        """The base URL of the bound server (no token)."""
        return f"http://{self._address}:{self._port}/"

    @property
    def closing(self) -> bool:
        """Whether the server is shutting down."""
        return self._closing

    async def start(self) -> int:
        """Bind the loopback socket and start accepting connections."""
        if self._server is not None:
            raise RuntimeError("web server already started")
        server = await asyncio.start_server(
            self._handle_connection,
            host=self._address,
            port=self._requested_port,
            limit=MAX_HEADER_BYTES,
            backlog=64,
        )
        sockets = server.sockets or ()
        if not sockets:  # pragma: no cover - start_server guarantees a socket
            server.close()
            raise RuntimeError("web server failed to bind a socket")
        self._server = server
        self._closing = False
        self._port = int(sockets[0].getsockname()[1])
        return self._port

    async def serve_forever(self) -> None:
        """Accept connections until close() is called."""
        if self._server is None:
            await self.start()
        server = self._server
        if server is None:  # pragma: no cover - start() raises instead
            return
        with contextlib.suppress(asyncio.CancelledError):
            await server.serve_forever()

    async def close(self) -> None:
        """Stop accepting, then cancel and drain open connections."""
        if self._closing and self._server is None:
            return
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- connection handling ------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            while not self._closing:
                try:
                    incoming = await self._read_request(reader)
                except TimeoutError:
                    return
                except HttpError as exc:
                    await self._write_response(writer, error_response(exc), keep_alive=False)
                    return
                if incoming is None:
                    return
                try:
                    keep_alive = await self._dispatch(writer, incoming.request, incoming.keep_alive)
                except (ConnectionError, asyncio.IncompleteReadError):
                    return
                if not keep_alive:
                    return
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - last-resort connection guard
            logger.exception("web connection failed")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._tasks.discard(task)

    async def _read_request(self, reader: asyncio.StreamReader) -> IncomingRequest | None:
        line = await self._read_line(reader)
        if line is None:
            return None
        text = line.decode("latin-1").strip("\r\n")
        if not text:
            line = await self._read_line(reader)
            if line is None:
                return None
            text = line.decode("latin-1").strip("\r\n")
        parts = text.split()
        if len(parts) != 3:
            raise BadRequest("malformed request line")
        method_text, target, version = parts
        try:
            method = Method(method_text.upper())
        except ValueError as exc:
            raise BadRequest(f"unsupported method: {method_text}") from exc
        if version not in {"HTTP/1.1", "HTTP/1.0"}:
            raise BadRequest(f"unsupported HTTP version: {version}")
        if not target.startswith("/"):
            raise BadRequest("only origin-form request targets are supported")
        path_text, _, query_text = target.partition("?")
        path = unquote(path_text)
        if "\x00" in path:
            raise BadRequest("invalid request target")
        query = {
            name: values[0] for name, values in parse_qs(query_text, keep_blank_values=True).items() if values
        }
        headers = await self._read_headers(reader)
        body = await self._read_body(reader, headers)
        keep_alive = _keep_alive(headers, version)
        request = WebRequest(
            method=method,
            path=path,
            query=query,
            headers=headers,
            body=body,
            http_version=version,
        )
        return IncomingRequest(request=request, keep_alive=keep_alive)

    async def _read_line(self, reader: asyncio.StreamReader) -> bytes | None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=self._idle_timeout)
        except ValueError as exc:  # LimitOverrunError is a ValueError subclass
            raise BadRequest("request header line too long") from exc
        if not raw:
            return None
        if len(raw) >= MAX_HEADER_BYTES:
            raise BadRequest("request header line too long")
        return raw

    async def _read_headers(self, reader: asyncio.StreamReader) -> dict[str, str]:
        headers: dict[str, str] = {}
        total = 0
        for _ in range(MAX_HEADERS + 1):
            line = await self._read_line(reader)
            if line is None:
                raise BadRequest("unexpected end of request headers")
            total += len(line)
            if total > MAX_HEADER_BYTES:
                raise BadRequest("request headers too large")
            if line in {b"\r\n", b"\n"}:
                return headers
            text = line.decode("latin-1").rstrip("\r\n")
            if text[:1] in {" ", "\t"}:
                raise BadRequest("header continuation lines are not supported")
            name, separator, value = text.partition(":")
            if not separator or not name.strip():
                raise BadRequest("malformed header line")
            key = name.strip().lower()
            cleaned = value.strip()
            previous = headers.get(key)
            headers[key] = cleaned if previous is None else f"{previous}, {cleaned}"
        raise BadRequest("too many request headers")

    async def _read_body(self, reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
        if headers.get("transfer-encoding", "").strip():
            raise BadRequest("transfer-encoding request bodies are not supported")
        raw_length = headers.get("content-length")
        if raw_length is None:
            return b""
        if "," in raw_length:
            raise BadRequest("ambiguous Content-Length header")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BadRequest("invalid Content-Length header") from exc
        if length < 0:
            raise BadRequest("invalid Content-Length header")
        if length > self._body_limit:
            raise PayloadTooLarge(f"request body exceeds {self._body_limit} bytes")
        if length == 0:
            return b""
        try:
            return await asyncio.wait_for(reader.readexactly(length), timeout=self._idle_timeout)
        except asyncio.IncompleteReadError as exc:
            raise BadRequest("incomplete request body") from exc

    # -- dispatch -----------------------------------------------------------

    async def _dispatch(
        self,
        writer: asyncio.StreamWriter,
        request: WebRequest,
        keep_alive: bool,
    ) -> bool:
        try:
            validate_request_origin(request.headers, port=self._port)
            require_token(request.headers, request.query, self._token)
        except HttpError as exc:
            await self._write_response(writer, error_response(exc), keep_alive=False)
            return False
        if request.method is Method.OPTIONS:
            try:
                allowed = self._registry.allowed(request.path)
            except HttpError as exc:
                await self._write_response(writer, error_response(exc), keep_alive=False)
                return False
            await self._write_response(
                writer,
                WebResponse(status=204, headers=(("allow", ", ".join(allowed)),)),
                keep_alive=keep_alive,
            )
            return keep_alive
        try:
            route, params = self._registry.match(request.method, request.path)
            if params:
                request = replace(request, path_params=params)
            result = await route.handler(self._service_provider(), request)
            if isinstance(result, SSEStream):
                await self._write_stream(writer, result)
                return False
            await self._write_response(
                writer,
                result,
                keep_alive=keep_alive,
                head=request.method is Method.HEAD,
            )
            return keep_alive
        except HttpError as exc:
            await self._write_response(
                writer,
                error_response(exc),
                keep_alive=False,
                head=request.method is Method.HEAD,
            )
            return False
        except (ConnectionError, asyncio.IncompleteReadError):
            raise
        except Exception:
            logger.exception("web route failed: %s %s", request.method, request.path)
            await self._write_response(
                writer,
                error_response(HttpError("internal server error")),
                keep_alive=False,
            )
            return False

    # -- responses ----------------------------------------------------------

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        response: WebResponse,
        *,
        keep_alive: bool,
        head: bool = False,
    ) -> None:
        body = response.body
        headers: list[tuple[str, str]] = [
            ("content-type", response.content_type),
            ("content-length", str(len(body))),
            *response.headers,
            *SECURITY_HEADERS,
            ("connection", "keep-alive" if keep_alive else "close"),
        ]
        payload = bytearray(_status_line(response.status))
        payload.extend(_encode_headers(_dedupe_headers(headers)))
        if not head:
            payload.extend(body)
        writer.write(bytes(payload))
        await asyncio.wait_for(writer.drain(), timeout=DEFAULT_WRITE_TIMEOUT)

    async def _write_stream(self, writer: asyncio.StreamWriter, stream: SSEStream) -> None:
        headers = [
            ("content-type", "text/event-stream; charset=utf-8"),
            *stream.headers,
            *SECURITY_HEADERS,
            ("connection", "close"),
            ("transfer-encoding", "chunked"),
        ]
        writer.write(_status_line(200) + _encode_headers(_dedupe_headers(headers)))
        await asyncio.wait_for(writer.drain(), timeout=DEFAULT_WRITE_TIMEOUT)
        try:
            async for chunk in stream.source:
                if self._closing:
                    break
                await self._write_chunk(writer, chunk if chunk else HEARTBEAT_FRAME)
            await self._write_chunk(writer, b"", final=True)
        finally:
            close = getattr(stream.source, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()

    async def _write_chunk(
        self,
        writer: asyncio.StreamWriter,
        data: bytes,
        *,
        final: bool = False,
    ) -> None:
        if final:
            writer.write(b"0\r\n\r\n")
        else:
            writer.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        await asyncio.wait_for(writer.drain(), timeout=DEFAULT_WRITE_TIMEOUT)


def _keep_alive(headers: dict[str, str], version: str) -> bool:
    tokens = {part.strip().lower() for part in headers.get("connection", "").split(",") if part.strip()}
    if "close" in tokens:
        return False
    if version == "HTTP/1.1":
        return True
    return "keep-alive" in tokens


__all__ = [
    "DEFAULT_IDLE_TIMEOUT",
    "DEFAULT_WRITE_TIMEOUT",
    "MAX_HEADER_BYTES",
    "IncomingRequest",
    "WebServer",
]
