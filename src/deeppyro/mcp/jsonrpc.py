"""JSON-RPC 2.0 framing helpers shared by the MCP transports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class McpProtocolError(RuntimeError):
    """The peer produced something that is not a valid JSON-RPC message."""


class JsonRpcError(McpProtocolError):
    """A JSON-RPC error object returned by the server."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data

    @classmethod
    def from_message(cls, message: dict[str, Any]) -> JsonRpcError:
        error = message.get("error")
        if not isinstance(error, dict):
            return cls(INTERNAL_ERROR, "malformed JSON-RPC error object")
        raw_code = error.get("code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = INTERNAL_ERROR
        return cls(code, str(error.get("message") or "error"), error.get("data"))


@dataclass(slots=True)
class McpResponse:
    """A JSON-RPC response, either a result or an error."""

    id: Any = None
    result: Any = None
    error: JsonRpcError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def unwrap(self) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


class RequestIds:
    """Hands out monotonically increasing JSON-RPC ids."""

    __slots__ = ("_next",)

    def __init__(self, start: int = 1) -> None:
        self._next = start

    def next(self) -> int:
        value = self._next
        self._next += 1
        return value


def request_message(request_id: int, method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification_message(method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def decode_message(raw: str | bytes) -> dict[str, Any]:
    """Parse one JSON-RPC message or raise :class:`McpProtocolError`."""

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise McpProtocolError(f"invalid JSON from MCP server: {exc}") from exc
    if not isinstance(message, dict):
        raise McpProtocolError("an MCP message must be a JSON object")
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise McpProtocolError(f"unsupported JSON-RPC version: {message.get('jsonrpc')!r}")
    return message


def is_response(message: dict[str, Any]) -> bool:
    """True when ``message`` answers a request (it has an id and a result or error)."""

    return "id" in message and ("result" in message or "error" in message)


def response_from_message(message: dict[str, Any]) -> McpResponse:
    if message.get("error") is not None:
        return McpResponse(id=message.get("id"), error=JsonRpcError.from_message(message))
    return McpResponse(id=message.get("id"), result=message.get("result"))
