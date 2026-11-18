"""Typed request/response boundaries for the web host.

HTTP is the untrusted edge: every byte that enters is parsed into frozen
dataclasses with validated fields, and every byte that leaves is built here.
Python code passes typed objects around; the helpers raise HttpError subclasses
that the server maps to JSON error responses with the right status code.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

#: The JSON value vocabulary used on the wire.
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

#: Default maximum accepted request body (1 MiB).
MAX_BODY_BYTES = 1024 * 1024

#: Maximum accepted user message length, in characters.
MAX_MESSAGE_CHARS = 100_000

#: Maximum accepted session id length.
MAX_SESSION_ID_CHARS = 128

#: Maximum accepted JSON error message length.
MAX_ERROR_CHARS = 4000


class HttpError(Exception):
    """Base class for an error the server renders as a JSON failure response."""

    status: int = 500
    code: str = "internal_error"
    allow: tuple[str, ...] = ()

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = (message or self.code)[:MAX_ERROR_CHARS]


class BadRequest(HttpError):
    """The request body or path was malformed or failed validation."""

    status = 400
    code = "bad_request"


class Unauthorized(HttpError):
    """The request did not carry the process bearer token."""

    status = 401
    code = "unauthorized"


class Forbidden(HttpError):
    """The request came from an origin or host the host does not serve."""

    status = 403
    code = "forbidden"


class NotFound(HttpError):
    """No route matched the request path."""

    status = 404
    code = "not_found"


class MethodNotAllowed(HttpError):
    """The path exists but does not accept this method."""

    status = 405
    code = "method_not_allowed"

    def __init__(self, allow: tuple[str, ...] = ()) -> None:
        self.allow = tuple(allow)
        super().__init__("method not allowed")


class Conflict(HttpError):
    """The operation conflicts with the current session state."""

    status = 409
    code = "conflict"


class PayloadTooLarge(HttpError):
    """The request body exceeded MAX_BODY_BYTES."""

    status = 413
    code = "payload_too_large"


class TooManyRequests(HttpError):
    """The bounded message queue is full."""

    status = 429
    code = "too_many_requests"


class ServiceUnavailable(HttpError):
    """The web surface is mounted but its session host is not running yet."""

    status = 503
    code = "service_unavailable"


class Method(StrEnum):
    """HTTP methods the host parses; not every route supports every method."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


@dataclass(frozen=True, slots=True)
class WebRequest:
    """One parsed HTTP request handed to a route handler."""

    method: Method
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes = b""
    http_version: str = "1.1"
    path_params: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        """One header value by case-insensitive name, if present."""
        return self.headers.get(name.lower())

    def json_object(self) -> dict[str, Any]:
        """Decode the body as a JSON object, rejecting anything else."""
        return decode_json_object(self.body)


@dataclass(frozen=True, slots=True)
class WebResponse:
    """One non-streaming HTTP response."""

    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SSEStream:
    """A server-sent-events response: an async byte iterator plus headers."""

    source: AsyncIterator[bytes]
    headers: tuple[tuple[str, str], ...] = ()


def json_response(payload: JsonValue, *, status: int = 200) -> WebResponse:
    """Build a JSON response with a stable compact encoding."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return WebResponse(
        status=status,
        body=body,
        content_type="application/json; charset=utf-8",
        headers=(("content-type", "application/json; charset=utf-8"),),
    )


def text_response(
    body: str,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
) -> WebResponse:
    """Build a textual response (HTML, JS, CSS)."""
    return WebResponse(
        status=status,
        body=body.encode("utf-8"),
        content_type=content_type,
        headers=(("content-type", content_type),),
    )


def error_response(error: HttpError) -> WebResponse:
    """Build the JSON error envelope for one HttpError."""
    error_payload: dict[str, JsonValue] = {"code": error.code, "message": error.message}
    if error.allow:
        error_payload["allow"] = cast(JsonValue, list(error.allow))
    payload: dict[str, JsonValue] = {"error": error_payload}
    headers: list[tuple[str, str]] = [("content-type", "application/json; charset=utf-8")]
    if error.allow:
        headers.append(("allow", ", ".join(error.allow)))
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return WebResponse(status=error.status, body=body, headers=tuple(headers))


def decode_json_object(body: bytes) -> dict[str, Any]:
    """Decode a request body that must be a JSON object."""
    if not body:
        return {}
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BadRequest(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise BadRequest("request body must be a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class CreateSessionRequest:
    """Body of POST /api/sessions."""

    cwd: str | None = None
    preset: str | None = None
    provider: str | None = None
    model: str | None = None
    title: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> CreateSessionRequest:
        """Validate a raw JSON object into this request type."""
        return cls(
            cwd=_optional_text(payload, "cwd", max_length=4096),
            preset=_optional_text(payload, "preset", max_length=128),
            provider=_optional_text(payload, "provider", max_length=128),
            model=_optional_text(payload, "model", max_length=256),
            title=_optional_text(payload, "title", max_length=512),
        )


@dataclass(frozen=True, slots=True)
class SendMessageRequest:
    """Body of POST /api/sessions/{id}/messages."""

    text: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> SendMessageRequest:
        """Validate the user message text."""
        value = payload.get("text")
        if not isinstance(value, str) or not value.strip():
            raise BadRequest('"text" must be a non-empty string')
        if len(value) > MAX_MESSAGE_CHARS:
            raise BadRequest(f'"text" must be at most {MAX_MESSAGE_CHARS} characters')
        return cls(text=value)


def parse_session_id(raw: str) -> str:
    """Validate a session id taken from a URL path segment."""
    session_id = raw.strip()
    if not session_id or len(session_id) > MAX_SESSION_ID_CHARS:
        raise BadRequest("invalid session id")
    if not all(character.isalnum() or character in "._-" for character in session_id):
        raise BadRequest("invalid session id")
    return session_id


def _optional_text(payload: Mapping[str, Any], key: str, *, max_length: int) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest(f'"{key}" must be a string')
    if len(value) > max_length:
        raise BadRequest(f'"{key}" must be at most {max_length} characters')
    return value


__all__ = [
    "MAX_BODY_BYTES",
    "MAX_MESSAGE_CHARS",
    "BadRequest",
    "Conflict",
    "CreateSessionRequest",
    "Forbidden",
    "HttpError",
    "JsonValue",
    "Method",
    "MethodNotAllowed",
    "NotFound",
    "PayloadTooLarge",
    "SSEStream",
    "SendMessageRequest",
    "ServiceUnavailable",
    "TooManyRequests",
    "Unauthorized",
    "WebRequest",
    "WebResponse",
    "decode_json_object",
    "error_response",
    "json_response",
    "parse_session_id",
    "text_response",
]
