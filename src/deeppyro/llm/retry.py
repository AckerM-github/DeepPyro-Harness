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
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Self, TypeVar

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

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay < 0 or self.max_delay < 0 or self.jitter < 0:
            raise ValueError("delays and jitter must be non-negative")

    # -- decisions ----------------------------------------------------------
    def should_retry(self, exc: BaseException) -> bool:
        """Only retryable llm failures are retried; cancellation is never retried."""
        if isinstance(exc, asyncio.CancelledError):
            return False
        if isinstance(exc, LlmError):
            return bool(exc.retryable)
        return False

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
