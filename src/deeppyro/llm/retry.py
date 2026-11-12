"""Retry policy used for the first byte of a model stream.

The DSH session log pins the retry shape (fixtures/records/by-type/llm_retry.jsonl):
mode "normal", maxRetries 5, retryable codes [EMPTY_RESPONSE, RATE_LIMIT, SERVER,
TIMEOUT, TRANSPORT], baseDelayMs 500, maxDelayMs 10000, jitter 0.1 and a measured
delayMs of 526.2 which is inside base .. base * (1 + jitter).  The Python port keeps the
same shape; only the totals differ (max_attempts counts attempts, not retries).

RetryPolicy is usable both directly and as the async context manager required by
INTERFACES section 5.9::

    async with RetryPolicy(max_attempts=3, base_delay=2.0) as retry:
        first_event = await retry.run(fetch_first_event)

For integration/replay tests the recorded DSH policy can be reproduced exactly with
RetryPolicy.from_policy(), which accepts the llm/retry record dict or the raw policyKey.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self, TypeVar

from deeppyro.core.errors import LlmError

T = TypeVar("T")

# HTTP statuses worth retrying: rate limiting, request timeout and transient 5xx.
RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Failure codes observed in the DSH retry policy (llm/retry policyKey).
RETRYABLE_CODES = frozenset({"EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"})


@dataclass(slots=True)
class RetryPolicy:
    """Bounded exponential backoff with multiplicative jitter.

    max_attempts counts total attempts (1 means "never retry").  retry numbers passed to
    delay_for are 1-based attempt numbers of the failure that just happened.
    """

    max_attempts: int = 3
    base_delay: float = 2.0
    max_delay: float = 30.0
    jitter: float = 0.1
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rand: Callable[[], float] = random.random
    # When set (e.g. from a recorded DSH policy), this code set is the authoritative
    # retryable decision and overrides LlmError.retryable.
    retryable_codes: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0 or self.max_delay < 0 or self.jitter < 0:
            raise ValueError("delays and jitter must be non-negative")

    @classmethod
    def from_policy(cls, policy: Mapping[str, Any] | Sequence[Any] | str) -> Self:
        """Rebuild a policy from a recorded DSH llm/retry policy.

        Accepts the dict shape {maxRetries, baseDelayMs, maxDelayMs, jitter, codes}
        (snake_case aliases are tolerated) and the raw policyKey stored in the session log,
        either as a JSON string or an already-parsed sequence:
        ["normal", 5, ["EMPTY_RESPONSE", ...], 500, 10000, 0.1].
        maxRetries is a retry *count*, so max_attempts = maxRetries + 1, and codes becomes
        the authoritative retryable set (see should_retry).  Missing keys fall back to the
        dataclass defaults; malformed values raise ValueError.
        """
        if isinstance(policy, str):
            try:
                policy = json.loads(policy)
            except ValueError as exc:
                raise ValueError(f"retry policy is not valid JSON: {policy[:120]!r}") from exc
        if isinstance(policy, Mapping):
            max_retries = _first(policy, "maxRetries", "max_retries")
            base_ms = _first(policy, "baseDelayMs", "base_delay_ms")
            max_ms = _first(policy, "maxDelayMs", "max_delay_ms")
            jitter = _first(policy, "jitter")
            codes = _first(policy, "codes", "retryableCodes", "retryable_codes")
        elif isinstance(policy, Sequence):
            items = list(policy)
            if len(items) < 6:
                raise ValueError(
                    "retry policyKey must be [mode, maxRetries, codes, baseDelayMs, maxDelayMs, jitter]"
                )
            _, max_retries, codes, base_ms, max_ms, jitter = items[:6]
        else:
            raise TypeError(f"retry policy must be a mapping, sequence or JSON string, got {policy!r}")

        kwargs: dict[str, Any] = {}
        if max_retries is not None:
            kwargs["max_attempts"] = _as_int(max_retries, "maxRetries") + 1
        if base_ms is not None:
            kwargs["base_delay"] = _as_number(base_ms, "baseDelayMs") / 1000.0
        if max_ms is not None:
            kwargs["max_delay"] = _as_number(max_ms, "maxDelayMs") / 1000.0
        if jitter is not None:
            kwargs["jitter"] = _as_number(jitter, "jitter")
        if codes is not None:
            if isinstance(codes, str) or not isinstance(codes, Iterable):
                raise TypeError(f"retry codes must be an iterable of strings, got {codes!r}")
            kwargs["retryable_codes"] = frozenset(_as_code(code) for code in codes)
        return cls(**kwargs)

    # -- decisions ----------------------------------------------------------
    def should_retry(self, exc: BaseException) -> bool:
        """Only retryable llm failures are retried; cancellation is never retried."""
        if isinstance(exc, asyncio.CancelledError):
            return False
        if self.retryable_codes is not None:
            code = getattr(exc, "code", None)
            return isinstance(code, str) and code in self.retryable_codes
        return isinstance(exc, LlmError) and bool(exc.retryable)

    def delay_for(self, retry: int, *, retry_after: float | None = None) -> float:
        """Seconds to sleep after failure number retry (1-based)."""
        if retry < 1:
            raise ValueError("retry must be >= 1")
        delay = self.base_delay * (2 ** (retry - 1))
        delay *= 1.0 + self.jitter * self.rand()
        if retry_after is not None and retry_after > delay:
            delay = retry_after
        return min(delay, self.max_delay)

    # -- execution ----------------------------------------------------------
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> None:
        return None

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        """Run operation, retrying retryable failures up to max_attempts times."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= self.max_attempts or not self.should_retry(exc):
                    raise
                retry_after = getattr(exc, "retry_after", None)
                await self.sleep(self.delay_for(attempt, retry_after=retry_after))


def _first(data: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return value


def _as_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")
    return float(value)


def _as_code(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"retry code must be a string, got {value!r}")
    if not value:
        raise ValueError("retry code must be a non-empty string")
    return value
