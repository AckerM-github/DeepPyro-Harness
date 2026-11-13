"""Error normalization tests for the shared HTTP/Anthropic helpers."""

from __future__ import annotations

import json

import pytest

from deeppyro.llm.errors import (
    ANTHROPIC_ERROR_TYPES,
    CODE_AUTH,
    CODE_BAD_REQUEST,
    CODE_CONTEXT_WINDOW_EXCEEDED,
    CODE_QUOTA,
    CODE_RATE_LIMIT,
    CODE_SERVER,
    CODE_TIMEOUT,
    anthropic_error,
    anthropic_error_info,
    code_for_status,
    is_context_window_message,
    parse_retry_after,
)


def test_anthropic_error_info_parses_official_shape() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "max_tokens: too large"},
        }
    )
    assert anthropic_error_info(body) == ("invalid_request_error", "max_tokens: too large")


def test_anthropic_error_info_handles_plain_body() -> None:
    error_type, message = anthropic_error_info("upstream is down")
    assert error_type == ""
    assert message == "upstream is down"


def test_anthropic_error_info_handles_empty_body() -> None:
    assert anthropic_error_info("  ") == ("", "")


def test_anthropic_error_info_handles_flat_error_body() -> None:
    assert anthropic_error_info(json.dumps({"type": "error", "message": "boom"})) == ("", "boom")


@pytest.mark.parametrize(
    ("status", "error_type", "code", "retryable"),
    [
        (401, "authentication_error", CODE_AUTH, False),
        (403, "permission_error", CODE_AUTH, False),
        (429, "rate_limit_error", CODE_RATE_LIMIT, True),
        (529, "overloaded_error", CODE_SERVER, True),
        (500, "api_error", CODE_SERVER, True),
        (400, "invalid_request_error", CODE_BAD_REQUEST, False),
        (402, "", CODE_QUOTA, False),
        (400, "", CODE_BAD_REQUEST, False),
        (500, "", CODE_SERVER, True),
        (429, "", CODE_RATE_LIMIT, True),
    ],
)
def test_anthropic_error_maps_status_and_type(
    status: int,
    error_type: str,
    code: str,
    retryable: bool,
) -> None:
    body = json.dumps({"type": "error", "error": {"type": error_type, "message": "nope"}})
    error = anthropic_error("claude", status=status, body=body)
    assert error.code == code
    assert error.retryable is retryable
    assert error.status == status
    assert error.provider == "claude"
    assert "claude" in str(error)
    assert "nope" in str(error)


def test_anthropic_error_type_wins_over_http_status() -> None:
    # A 500 carrying invalid_request_error is a user error, not a transient failure.
    error = anthropic_error(
        "claude",
        status=500,
        error_type="invalid_request_error",
        message="bad tools",
    )
    assert error.code == CODE_BAD_REQUEST
    assert error.retryable is False


def test_anthropic_error_without_status_is_retryable_server_error() -> None:
    error = anthropic_error("claude", error_type="", message="connection reset")
    assert error.code == CODE_SERVER
    assert error.retryable is True
    assert error.status is None


def test_anthropic_error_detects_context_window_phrasing() -> None:
    error = anthropic_error(
        "claude",
        status=400,
        error_type="invalid_request_error",
        message="prompt is too long: 210000 tokens > 200000 maximum",
    )
    assert error.code == CODE_CONTEXT_WINDOW_EXCEEDED
    assert error.retryable is False


def test_anthropic_error_reads_body_without_explicit_message() -> None:
    body = json.dumps(
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "maximum context length exceeded",
            },
        }
    )
    error = anthropic_error("claude", status=400, body=body)
    assert error.code == CODE_CONTEXT_WINDOW_EXCEEDED
    assert "maximum context length" in str(error)


def test_anthropic_error_parses_retry_after_seconds() -> None:
    error = anthropic_error("claude", status=429, headers={"retry-after": "7"}, body="")
    assert error.retry_after == 7.0


def test_anthropic_error_parses_retry_after_http_date() -> None:
    error = anthropic_error(
        "claude",
        status=429,
        headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"},
    )
    assert error.retry_after == 0.0  # a date in the past clamps to zero


def test_anthropic_error_keeps_body_for_diagnostics() -> None:
    body = json.dumps({"type": "error", "error": {"type": "api_error", "message": "x"}})
    error = anthropic_error("claude", status=500, body=body)
    assert error.body == body


def test_anthropic_error_table_covers_documented_types() -> None:
    assert set(ANTHROPIC_ERROR_TYPES) == {
        "authentication_error",
        "permission_error",
        "rate_limit_error",
        "invalid_request_error",
        "overloaded_error",
        "api_error",
    }


@pytest.mark.parametrize(
    "message",
    [
        "context window exceeded",
        "the context length is too long",
        "maximum context reached",
        "prompt is too long",
        "too many tokens",
        "input is too long",
        "request exceeds the maximum",
    ],
)
def test_is_context_window_message_markers(message: str) -> None:
    assert is_context_window_message(message)


def test_is_context_window_message_rejects_other_errors() -> None:
    assert not is_context_window_message("invalid api key")
    assert not is_context_window_message("")


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, CODE_RATE_LIMIT),
        (408, CODE_TIMEOUT),
        (401, CODE_AUTH),
        (403, CODE_AUTH),
        (404, "NOT_FOUND"),
        (500, CODE_SERVER),
        (503, CODE_SERVER),
        (400, CODE_BAD_REQUEST),
        (422, CODE_BAD_REQUEST),
        (302, "HTTP_302"),
    ],
)
def test_code_for_status(status: int, code: str) -> None:
    assert code_for_status(status) == code


def test_parse_retry_after_rejects_garbage() -> None:
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("-3") == 0.0


def test_failure_payload_uses_the_classified_code() -> None:
    error = anthropic_error("claude", error_type="", message="")
    failure = error.failure()
    assert failure["code"] == CODE_SERVER
    assert failure["message"] == str(error)
