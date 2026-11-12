"""RetryPolicy tests: backoff arithmetic, retryability rules, cancellation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from deeppyro.core.errors import LlmError
from deeppyro.llm.errors import LlmRequestError
from deeppyro.llm.retry import RETRYABLE_CODES, RetryPolicy

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

requires_fixtures = pytest.mark.skipif(
    not FIXTURES.is_dir(),
    reason="privacy: real-session fixtures are not committed; run node tools/gen_fixtures.mjs",
)


def policy_with_sleeps(**kwargs: object) -> tuple[RetryPolicy, list[float]]:
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    kwargs.setdefault("rand", lambda: 0.5)
    return RetryPolicy(sleep=sleep, **kwargs), sleeps  # type: ignore[arg-type]


def test_delay_grows_exponentially_with_half_jitter() -> None:
    policy, _ = policy_with_sleeps(base_delay=2.0, jitter=0.1)
    assert policy.delay_for(1) == pytest.approx(2.1)
    assert policy.delay_for(2) == pytest.approx(4.2)
    assert policy.delay_for(3) == pytest.approx(8.4)


def test_delay_is_capped() -> None:
    policy, _ = policy_with_sleeps(base_delay=2.0, max_delay=5.0, jitter=0.0)
    assert policy.delay_for(3) == 5.0


def test_delay_honours_retry_after_when_larger() -> None:
    policy, _ = policy_with_sleeps(base_delay=2.0, jitter=0.0)
    assert policy.delay_for(1, retry_after=7.0) == 7.0
    assert policy.delay_for(1, retry_after=0.5) == 2.0


def test_retry_after_is_capped_by_max_delay() -> None:
    policy, _ = policy_with_sleeps(base_delay=2.0, max_delay=10.0, jitter=0.0)
    assert policy.delay_for(1, retry_after=100.0) == 10.0


def test_retryability_rules() -> None:
    policy = RetryPolicy()
    assert policy.should_retry(LlmError("x", retryable=True))
    assert not policy.should_retry(LlmError("x", retryable=False))
    assert not policy.should_retry(ValueError("x"))
    assert not policy.should_retry(asyncio.CancelledError())


def test_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay=-1)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=-0.1)
    with pytest.raises(ValueError):
        RetryPolicy().delay_for(0)


async def test_run_retries_retryable_failures_then_succeeds() -> None:
    policy, sleeps = policy_with_sleeps(base_delay=2.0, jitter=0.1)
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise LlmError("transient", retryable=True)
        return "ok"

    assert await policy.run(operation) == "ok"
    assert attempts == 3
    assert sleeps == pytest.approx([2.1, 4.2])


async def test_run_does_not_retry_non_retryable() -> None:
    policy, sleeps = policy_with_sleeps()
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise LlmError("bad request", retryable=False)

    with pytest.raises(LlmError):
        await policy.run(operation)
    assert attempts == 1
    assert sleeps == []


async def test_run_exhausts_attempts() -> None:
    policy, sleeps = policy_with_sleeps(max_attempts=3, base_delay=1.0, jitter=0.0)
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise LlmError("still down", retryable=True)

    with pytest.raises(LlmError, match="still down"):
        await policy.run(operation)
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


async def test_run_propagates_cancellation_without_sleeping() -> None:
    policy, sleeps = policy_with_sleeps()
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await policy.run(operation)
    assert attempts == 1
    assert sleeps == []


async def test_run_uses_retry_after_from_error() -> None:
    policy, sleeps = policy_with_sleeps(base_delay=1.0, jitter=0.0)
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LlmRequestError("slow down", code="RATE_LIMIT", retryable=True, retry_after=12.0)
        return "ok"

    assert await policy.run(operation) == "ok"
    assert sleeps == [12.0]


async def test_context_manager_returns_policy() -> None:
    policy = RetryPolicy()
    async with policy as inner:
        assert inner is policy


def test_from_policy_maps_recorded_shape() -> None:
    policy = RetryPolicy.from_policy(
        {
            "maxRetries": 5,
            "baseDelayMs": 500,
            "maxDelayMs": 10000,
            "jitter": 0.1,
            "codes": ["EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"],
        }
    )
    assert policy.max_attempts == 6
    assert policy.base_delay == 0.5
    assert policy.max_delay == 10.0
    assert policy.jitter == 0.1
    assert policy.retryable_codes == RETRYABLE_CODES


@requires_fixtures
def test_from_policy_reads_fixture_policy_key() -> None:
    record = json.loads(
        (FIXTURES / "records" / "by-type" / "llm_retry.jsonl").read_text("utf-8").splitlines()[0]
    )
    raw_key = record["data"]["policyKey"]
    from_string = RetryPolicy.from_policy(raw_key)
    from_sequence = RetryPolicy.from_policy(json.loads(raw_key))
    for policy in (from_string, from_sequence):
        assert policy.max_attempts == 6  # maxRetries 5 -> 6 total attempts
        assert policy.base_delay == 0.5
        assert policy.max_delay == 10.0
        assert policy.jitter == 0.1
        assert policy.retryable_codes == RETRYABLE_CODES


def test_from_policy_codes_are_authoritative_for_retry() -> None:
    policy = RetryPolicy(retryable_codes=frozenset({"SERVER"}))
    assert policy.should_retry(LlmRequestError("boom", code="SERVER", retryable=False)) is True
    assert policy.should_retry(LlmRequestError("boom", code="PROTOCOL", retryable=True)) is False
    assert policy.should_retry(asyncio.CancelledError()) is False
    assert policy.should_retry(LlmError("plain", retryable=True)) is False


async def test_from_policy_policy_drives_run() -> None:
    policy = RetryPolicy.from_policy(
        {"maxRetries": 5, "baseDelayMs": 500, "maxDelayMs": 10000, "jitter": 0.1}
    )
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    policy.sleep = sleep
    policy.rand = lambda: 0.5
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise LlmError("transient", retryable=True)
        return "ok"

    assert await policy.run(operation) == "ok"
    assert attempts == 3
    assert sleeps == pytest.approx([0.525, 1.05])


def test_from_policy_rejects_bad_shapes() -> None:
    with pytest.raises(TypeError):
        RetryPolicy.from_policy({"maxRetries": "5"})
    with pytest.raises(TypeError):
        RetryPolicy.from_policy({"codes": "SERVER"})
    with pytest.raises(TypeError):
        RetryPolicy.from_policy(3)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RetryPolicy.from_policy({"maxRetries": -1})
    with pytest.raises(ValueError):
        RetryPolicy.from_policy(["normal", 5])
    with pytest.raises(ValueError):
        RetryPolicy.from_policy("{not json}")


@requires_fixtures
def test_retryable_codes_match_fixture_policy_key() -> None:
    """fixtures pin the retry policy key of the DSH log; our code set must agree."""
    record = json.loads(
        (FIXTURES / "records" / "by-type" / "llm_retry.jsonl").read_text("utf-8").splitlines()[0]
    )
    policy_key = json.loads(record["data"]["policyKey"])
    mode, max_retries, codes, base_ms, max_ms, jitter = policy_key
    assert mode == "normal"
    assert max_retries == 5
    assert set(codes) == set(RETRYABLE_CODES)
    assert (base_ms, max_ms, jitter) == (500, 10000, 0.1)
    delay_ms = record["data"]["delayMs"]
    assert 500 <= delay_ms <= 500 * 1.1
