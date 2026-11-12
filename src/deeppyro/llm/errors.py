"""Error types raised by the llm layer.

deeppyro.core.errors defines the cross-module contract: LlmError(message, provider=,
status=, retryable=).  This module refines it with the machine-readable failure code the
DSH session logs record (llm/retry failure.code: RATE_LIMIT / SERVER / TIMEOUT /
TRANSPORT / EMPTY_RESPONSE) plus transport hints such as Retry-After.
"""

from __future__ import annotations

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
