"""Transports for the MCP client: newline-delimited stdio and streamable HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from typing import Any

import httpx

from .jsonrpc import decode_message, is_response, response_from_message
from .types import McpServerConfig

#: How long ``close`` waits for the child process to exit before killing it.
TERMINATE_TIMEOUT = 5.0

#: Upper bound on the error body echoed back in transport error messages.
BODY_LIMIT = 400

DEFAULT_HEADERS = {"accept": "application/json, text/event-stream"}

#: Environment names upstream strips before spawning a stdio server.
_SECRET_NAME = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.IGNORECASE)
_SCRUBBED_PREFIXES = ("DSH_", "DEEPPYRO_", "DPYRO_")


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The parent environment minus credentials and host namespaces, plus ``extra``."""

    env = {
        name: value
        for name, value in os.environ.items()
        if not _SECRET_NAME.search(name) and not name.startswith(_SCRUBBED_PREFIXES)
    }
    if extra:
        env.update({str(name): str(value) for name, value in extra.items()})
    return env


class McpTransportError(RuntimeError):
    """The transport could not deliver or read a message."""


class McpTimeoutError(McpTransportError):
    """The peer did not answer within the configured timeout."""


class StdioTransport:
    """Newline-delimited JSON over a child process' stdin/stdout."""

    def __init__(self, config: McpServerConfig, *, logger: logging.Logger | None = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("deeppyro.mcp.stdio")
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future[Any]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._closed = False

    @property
    def started(self) -> bool:
        return self._process is not None

    async def start(self) -> None:
        if self._process is not None:
            return
        argv = [self.config.command, *self.config.args]
        env = scrubbed_env(self.config.env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.config.cwd or None,
            )
        except OSError as exc:
            raise McpTransportError(f"could not start {self.config.command!r}: {exc}") from exc
        self._tasks = [
            asyncio.create_task(self._read_stdout(), name="mcp-stdio-stdout"),
            asyncio.create_task(self._read_stderr(), name="mcp-stdio-stderr"),
            asyncio.create_task(self._watch_exit(), name="mcp-stdio-exit"),
        ]

    async def request(self, message: dict[str, Any], *, timeout: float) -> Any:
        """Send a request message and return the decoded response."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[message.get("id")] = future
        try:
            await self._write(message)
        except BaseException:
            self._pending.pop(message.get("id"), None)
            raise
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            self._pending.pop(message.get("id"), None)
            raise McpTimeoutError(f"no response from MCP server within {timeout:g}s") from None

    async def notify(self, message: dict[str, Any], *, timeout: float = 5.0) -> None:
        await self._write(message)

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise McpTransportError("the MCP server process is not running")
        if process.returncode is not None:
            raise McpTransportError(f"the MCP server exited with code {process.returncode}")
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise McpTransportError(f"the MCP server closed its stdin: {exc}") from exc

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        while True:
            try:
                line = await process.stdout.readline()
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as exc:  # noqa: BLE001 - the reader must not die silently
                self._fail_pending(McpTransportError(f"could not read MCP stdout: {exc}"))
                return
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                message = decode_message(text)
            except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the reader
                self.logger.warning("mcp %s: discarding malformed frame: %s", self.config.name, exc)
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict[str, Any]) -> None:
        if not is_response(message):
            self.logger.debug("mcp %s: ignoring server message %s", self.config.name, message.get("method"))
            return
        future = self._pending.pop(message.get("id"), None)
        if future is None:
            self.logger.debug("mcp %s: response for unknown id %r", self.config.name, message.get("id"))
            return
        if not future.done():
            future.set_result(response_from_message(message))

    async def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self.logger.debug("mcp %s stderr: %s", self.config.name, text)

    async def _watch_exit(self) -> None:
        process = self._process
        if process is None:
            return
        code = await process.wait()
        self._fail_pending(McpTransportError(f"the MCP server exited with code {code}"))

    def _fail_pending(self, error: BaseException) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        self._process = None
        if process is not None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), TERMINATE_TIMEOUT)
            except (TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self._fail_pending(McpTransportError("the MCP transport was closed"))


class HttpTransport:
    """Streamable HTTP: POST JSON-RPC messages, accept JSON or SSE responses."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        client: httpx.AsyncClient | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger("deeppyro.mcp.http")
        self._client = client
        self._owns_client = client is None
        self.session_id: str | None = None
        self._closed = False

    async def start(self) -> None:
        if self._client is None:
            headers = {**DEFAULT_HEADERS, "content-type": "application/json"}
            self._client = httpx.AsyncClient(headers=headers, timeout=self.config.startup_timeout)

    @property
    def _request_headers(self) -> dict[str, str]:
        headers = {**DEFAULT_HEADERS, "content-type": "application/json", **self.config.headers}
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    async def request(self, message: dict[str, Any], *, timeout: float) -> Any:
        response = await self._post(message, timeout=timeout, expect_response=True)
        messages = _messages_from_response(response)
        request_id = message.get("id")
        for candidate in messages:
            if is_response(candidate) and candidate.get("id") == request_id:
                return response_from_message(candidate)
        raise McpTransportError(f"the MCP server did not answer request {request_id!r}")

    async def notify(self, message: dict[str, Any], *, timeout: float = 5.0) -> None:
        await self._post(message, timeout=timeout, expect_response=False)

    async def _post(
        self, message: dict[str, Any], *, timeout: float, expect_response: bool
    ) -> httpx.Response:
        if self._client is None:
            raise McpTransportError("the HTTP transport is not started")
        try:
            response = await self._client.post(
                self.config.url,
                json=message,
                headers=self._request_headers,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise McpTransportError(f"could not reach {self.config.url}: {exc}") from exc
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self.session_id = session_id
        if response.status_code >= 400:
            raise McpTransportError(
                f"the MCP server returned HTTP {response.status_code}: {response.text[:BODY_LIMIT]}"
            )
        if expect_response and response.status_code in (202, 204):
            raise McpTransportError(
                f"the MCP server returned HTTP {response.status_code} with no JSON-RPC response"
            )
        return response

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None and self._owns_client:
            with contextlib.suppress(Exception):
                await self._client.aclose()
        self._client = None


def _messages_from_response(response: httpx.Response) -> list[dict[str, Any]]:
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        return _messages_from_sse(response.text)
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _messages_from_sse(text: str) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        payload = stripped[len("data:") :].strip()
        if not payload:
            continue
        try:
            message = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(message, dict):
            messages.append(message)
    return messages


def make_transport(config: McpServerConfig, *, logger: logging.Logger | None = None) -> Any:
    """Build the transport a ``McpServerConfig`` asks for."""

    if config.streamable_http:
        return HttpTransport(config, logger=logger)
    return StdioTransport(config, logger=logger)
