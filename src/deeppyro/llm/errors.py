"""Error types raised by the llm layer.

deeppyro.core.errors defines the cross-module contract: LlmError(message, provider=,
status=, retryable=).  This module refines it with the machine-readable failure code the
DSH session logs record (llm/retry failure.code: RATE_LIMIT / SERVER / TIMEOUT /
TRANSPORT / EMPTY_RESPONSE) plus transport hints such as Retry-After.

It also hosts the small HTTP-error normalization helpers shared by the streaming transport
(openai_compat.py) and catalog discovery (discovery.py): status -> code/message/retryable,
Retry-After parsing and error-body summarising. Keeping them here means a listing failure
and a stream failure are classified identically.
"""

from __future__ import annotations

import email.utils
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from deeppyro.core.errors import LlmError

# Machine-readable failure codes, aligned with the codes observed in fixtures
# records/by-type/llm_retry.jsonl (policyKey retryable-code set).
CODE_EMPTY_RESPONSE = "EMPTY_RESPONSE"
CODE_RATE_LIMIT = "RATE_LIMIT"
CODE_SERVER = "SERVER"
CODE_TIMEOUT = "TIMEOUT"
CODE_TRANSPORT = "TRANSPORT"
CODE_PROTOCOL = "PROTOCOL"
CODE_AUTH = "AUTH"
CODE_BAD_REQUEST = "BAD_REQUEST"
CODE_NOT_FOUND = "NOT_FOUND"
CODE_QUOTA = "QUOTA"
CODE_CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
CODE_STREAM_CLOSED = "STREAM_CLOSED"

#: Anthropic error `type` values -> (failure code, retryable).
#: See <https://docs.anthropic.com/en/api/errors>; the official Python SDK maps the same
#: set. Anything not listed here falls back to the HTTP status classification below.
ANTHROPIC_ERROR_TYPES: dict[str, tuple[str, bool]] = {
    "authentication_error": (CODE_AUTH, False),
    "permission_error": (CODE_AUTH, False),
    "rate_limit_error": (CODE_RATE_LIMIT, True),
    "invalid_request_error": (CODE_BAD_REQUEST, False),
    "overloaded_error": (CODE_SERVER, True),
    "api_error": (CODE_SERVER, True),
}

#: Phrases Anthropic uses for "your prompt does not fit" (status 400). Those are user
#: errors (shrink the history/context window), never retried automatically.
_CONTEXT_WINDOW_MARKERS = (
    "context window",
    "context length",
    "maximum context",
    "prompt is too long",
    "too many tokens",
    "input is too long",
    "exceeds the maximum",
)


def failure_dict(code: str, message: str) -> dict[str, str]:
    """Build a core.protocols.Finish.failure payload; fixtures use {message, code}."""
    return {"message": message, "code": code}


class LlmRequestError(LlmError):
    """LlmError carrying a failure code and optional HTTP/retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status: int | None = None,
        retryable: bool = False,
        code: str = "",
        retry_after: float | None = None,
        body: str = "",
    ) -> None:
        super().__init__(message, provider=provider, status=status, retryable=retryable)
        self.code = code
        self.retry_after = retry_after
        self.body = body

    def failure(self) -> dict[str, Any]:
        """Failure payload suitable for Finish(kind="error", failure=...)."""
        return failure_dict(self.code or CODE_PROTOCOL, str(self))


class LlmHttpError(LlmRequestError):
    """The server answered with an HTTP error status."""


def code_for_status(status: int) -> str:
    """Map an HTTP status to the stable failure code used in session logs."""
    if status == 429:
        return CODE_RATE_LIMIT
    if status == 408:
        return CODE_TIMEOUT
    if status in (401, 403):
        return CODE_AUTH
    if status == 404:
        return CODE_NOT_FOUND
    if 500 <= status < 600:
        return CODE_SERVER
    if 400 <= status < 500:
        return CODE_BAD_REQUEST
    return f"HTTP_{status}"


def http_error(
    provider: str,
    status: int,
    *,
    headers: Mapping[str, str] | None = None,
    body: str = "",
) -> LlmHttpError:
    """Build the LlmHttpError for a non-2xx provider response.

    Retryability follows RetryPolicy.RETRYABLE_HTTP_STATUSES: rate limiting, request
    timeout and transient 5xx are retryable, other 4xx are not (an auth or bad-request
    failure will not fix itself). Retry-After is carried on the error for the retry layer.
    """
    # Local import keeps deeppyro.llm.errors free of an import cycle at module load.
    from deeppyro.llm.retry import RETRYABLE_HTTP_STATUSES

    retryable = status in RETRYABLE_HTTP_STATUSES or 500 <= status < 600
    detail = extract_error_message(body)
    message = f"{provider}: HTTP {status}" + (f": {detail}" if detail else "")
    retry_after = parse_retry_after(headers.get("retry-after") if headers is not None else None)
    return LlmHttpError(
        message,
        provider=provider,
        status=status,
        retryable=retryable,
        code=code_for_status(status),
        retry_after=retry_after,
        body=body,
    )


def is_context_window_message(message: str) -> bool:
    """True when an error message means "prompt exceeds the model context window"."""
    lowered = message.lower()
    return any(marker in lowered for marker in _CONTEXT_WINDOW_MARKERS)


def anthropic_error_info(body: str) -> tuple[str, str]:
    """Extract (error type, message) from an Anthropic error body.

    Anthropic returns {"type": "error", "error": {"type": "...", "message": "..."}}.
    Returns ("", "") for bodies that do not follow that shape.
    """
    if not body.strip():
        return "", ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return "", shorten(body)
    if not isinstance(parsed, dict):
        return "", shorten(body)
    error = parsed.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        message = error.get("message")
        return (
            error_type if isinstance(error_type, str) else "",
            message if isinstance(message, str) and message else extract_error_message(body),
        )
    message = parsed.get("message")
    error_type = parsed.get("type")
    return (
        error_type if isinstance(error_type, str) and error_type != "error" else "",
        message if isinstance(message, str) and message else "",
    )


def anthropic_error(
    provider: str,
    *,
    status: int | None = None,
    headers: Mapping[str, str] | None = None,
    body: str = "",
    error_type: str = "",
    message: str = "",
) -> LlmRequestError:
    """Normalize an Anthropic API failure to an LlmRequestError.

    Classification order (most specific first): context-window phrasing -> Anthropic
    error type -> HTTP status -> no status at all (`SERVER`, retryable). Retryability is
    therefore independent of the status table when the server sends an explicit type.
    Retry-After is parsed from the response headers.
    """
    # Local import keeps deeppyro.llm.errors free of an import cycle at module load.
    from deeppyro.llm.retry import RETRYABLE_HTTP_STATUSES

    body_type, body_message = anthropic_error_info(body)
    error_type = error_type or body_type
    message = message or body_message
    retry_after = parse_retry_after(headers.get("retry-after") if headers is not None else None)
    if message and is_context_window_message(message):
        code, retryable = CODE_CONTEXT_WINDOW_EXCEEDED, False
    elif error_type in ANTHROPIC_ERROR_TYPES:
        code, retryable = ANTHROPIC_ERROR_TYPES[error_type]
    elif status is None:
        code, retryable = CODE_SERVER, True
    elif status == 402:
        code, retryable = CODE_QUOTA, False
    else:
        code = code_for_status(status)
        retryable = status in RETRYABLE_HTTP_STATUSES or 500 <= status < 600
    if status is not None:
        text = f"{provider}: HTTP {status}" + (f": {message}" if message else "")
    else:
        text = f"{provider}: provider error" + (f": {message}" if message else "")
    return LlmRequestError(
        text,
        provider=provider,
        status=status,
        retryable=retryable,
        code=code,
        retry_after=retry_after,
        body=body,
    )


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After: delta-seconds or an HTTP date."""
    if not value:
        return None
    candidate = value.strip()
    try:
        return max(0.0, float(candidate))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def shorten(text: str, limit: int = 400) -> str:
    """Collapse an untrusted response body to a log-safe, single-ish line preview."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def extract_error_message(body: str) -> str:
    """Best-effort human message from an OpenAI/Anthropic style error body."""
    if not body.strip():
        return ""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return shorten(body)
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if isinstance(message, str) and message:
                return message
        for key in ("message", "detail", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
    return shorten(body)


__all__ = [
    "ANTHROPIC_ERROR_TYPES",
    "CODE_AUTH",
    "CODE_BAD_REQUEST",
    "CODE_CONTEXT_WINDOW_EXCEEDED",
    "CODE_EMPTY_RESPONSE",
    "CODE_NOT_FOUND",
    "CODE_PROTOCOL",
    "CODE_QUOTA",
    "CODE_RATE_LIMIT",
    "CODE_SERVER",
    "CODE_STREAM_CLOSED",
    "CODE_TIMEOUT",
    "CODE_TRANSPORT",
    "LlmHttpError",
    "LlmRequestError",
    "anthropic_error",
    "anthropic_error_info",
    "code_for_status",
    "extract_error_message",
    "failure_dict",
    "http_error",
    "is_context_window_message",
    "parse_retry_after",
    "shorten",
]
