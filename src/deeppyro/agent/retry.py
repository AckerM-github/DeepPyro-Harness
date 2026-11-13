"""Retry decisions for failed model requests.

DSH retries a failed provider request at a durable agent-step boundary: the retry
is logged before it waits, so a crash never leaves an invisible pending retry.
The session log pins the shape (fixtures/records/by-type/llm_retry.jsonl):

    {"retryId": "abfe5aad-...", "turn": 3, "step": 1, "provider": "deepseek-...",
     "mode": "normal",
     "policyKey": "[\"normal\",5,[\"EMPTY_RESPONSE\",\"RATE_LIMIT\",\"SERVER\",\"TIMEOUT\",\"TRANSPORT\"],500,10000,0.1]",
     "retry": 1, "maxRetries": 5, "delayMs": 526.209177272178,
     "failure": {"message": "502 ...", "code": "SERVER"}}

followed by llm/retry-started {retryId, turn, step, retry}.  This module is the pure
decision function: it computes whether to retry, the retry id, the backoff delay,
and the two record payloads; it never sleeps, appends, or performs I/O.

The rules are taken from @deepseek-ai/dsh-llm-retry's recovery listener:

* No policy means no recovery (the failure delegates unchanged).
* normal mode retries only failures whose code is in the policy's retryable set and
  only while previous.retry < maxRetries.
* always mode retries every failure without a budget; callers that own a downstream
  recovery chain first give it a chance to answer, then fall back here.
* The provider's Retry-After (failure.providerRetryAfterMs) wins over local backoff
  when it is a finite positive value; a value above maxDelayMs aborts normal mode
  (the provider asked for a wait the policy forbids) while always mode ignores it
  and uses local backoff.
* Local backoff is min(initial * 2 ** min(retry - 1, 1024), maxDelay) scaled by a
  symmetric jitter 1 - jitterRatio + 2 * jitterRatio * rand(), then capped at
  maxDelay again.
* The retry id is reused from the previous retry of the same provider+policy chain,
  so every retry of one chain shares one id.
"""

from __future__ import annotations

import json
import math
import random
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from deeppyro.llm.retry import RETRYABLE_CODES, RetryPolicy

# DSH defaults for a provider that omits retryPolicy.backoff.
DEFAULT_MAX_RETRIES = 5
DEFAULT_INITIAL_DELAY_MS = 500.0
DEFAULT_MAX_DELAY_MS = 10000.0
DEFAULT_JITTER_RATIO = 0.1
MAX_EXPONENT = 1024

_MISSING = object()


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """One computed retry decision plus the records the caller should append.

    action is "retry" when the loop should append record, wait delay_ms, append
    started_record, and re-run the failed step; action is "stop" when the failure
    must propagate (record and started_record are then None).
    """

    action: Literal["retry", "stop"]
    retry: int
    retry_id: str | None
    delay_ms: float
    mode: str
    policy_key: str
    record: dict[str, Any] | None
    started_record: dict[str, Any] | None

    @property
    def should_retry(self) -> bool:
        """Whether the caller should schedule another attempt."""
        return self.action == "retry"


def decide_retry(
    *,
    failure: Mapping[str, Any],
    policy: Any,
    previous: Any | None,
    provider: str,
    turn: int,
    step: int,
    retry_policy: Any | None = None,
    now: float | None = None,
    rand: Callable[[], float] | None = None,
) -> RetryDecision:
    """Decide whether one failed model request is retried.

    failure is the LlmFailure-shaped dict (message, code, optional
    providerRetryAfterMs); policy is a DSH retry policy mapping, a policyKey JSON
    string/sequence, or a deeppyro.llm.retry.RetryPolicy.  previous is the
    llmRetry projection entry for the provider+policy chain (mapping or object with
    retry/retryId, e.g. from the last llm/retry record).  retry_policy is the
    fallback policy used when policy is None, and supplies retryable codes/backoff
    for a partial policy mapping.

    now is accepted so the loop can pass its clock; the decision itself is
    timestamp-free because the session envelope stamps the record time.
    """
    failure_data = _failure(failure)
    provider_name = _provider(provider)
    turn_value = _integer(turn, "turn")
    step_value = _integer(step, "step")
    previous_retry, previous_id = _previous(previous)
    config = _normalize_policy(policy, retry_policy)
    random_value = _random(rand)
    if config is None:
        return _stop(previous_retry, previous_id, mode="", policy_key="")
    policy_key = _policy_key(config)
    code = failure_data["code"]
    if config.mode == "normal":
        if code not in config.retryable_codes:
            return _stop(previous_retry, previous_id, mode=config.mode, policy_key=policy_key)
        if config.max_retries is not None and previous_retry >= config.max_retries:
            return _stop(previous_retry, previous_id, mode=config.mode, policy_key=policy_key)
    retry = previous_retry + 1
    retry_id = previous_id if previous_id is not None else str(uuid.uuid4())
    delay_ms = _delay_ms(config, retry, failure_data.get("providerRetryAfterMs"), random_value)
    if delay_ms is None:
        return _stop(previous_retry, previous_id, mode=config.mode, policy_key=policy_key)
    record: dict[str, Any] = {
        "retryId": retry_id,
        "turn": turn_value,
        "step": step_value,
        "provider": provider_name,
        "mode": config.mode,
        "policyKey": policy_key,
        "retry": retry,
    }
    if config.mode == "normal":
        record["maxRetries"] = config.max_retries
    record["delayMs"] = _number_out(delay_ms)
    record["failure"] = dict(failure_data)
    started_record = {
        "retryId": retry_id,
        "turn": turn_value,
        "step": step_value,
        "retry": retry,
    }
    return RetryDecision(
        action="retry",
        retry=retry,
        retry_id=retry_id,
        delay_ms=delay_ms,
        mode=config.mode,
        policy_key=policy_key,
        record=record,
        started_record=started_record,
    )


@dataclass(frozen=True, slots=True)
class _Policy:
    """Normalized provider retry policy (durations in milliseconds)."""

    mode: str
    max_retries: int | None
    retryable_codes: frozenset[str]
    initial_delay_ms: float
    max_delay_ms: float
    jitter_ratio: float


def _normalize_policy(policy: Any, retry_policy: Any | None) -> _Policy | None:
    if policy is None:
        if retry_policy is None:
            return None
        return _from_retry_policy(retry_policy)
    if isinstance(policy, RetryPolicy):
        return _from_retry_policy(policy)
    fallback = _as_fallback(retry_policy)
    if isinstance(policy, str):
        try:
            decoded = json.loads(policy)
        except ValueError as exc:
            raise ValueError(f"retry policyKey is not valid JSON: {policy[:120]!r}") from exc
        return _from_policy_key(decoded)
    if isinstance(policy, Mapping):
        return _from_mapping(policy, fallback)
    if isinstance(policy, Sequence) and not isinstance(policy, (str, bytes)):
        return _from_policy_key(policy)
    raise TypeError(
        "retry policy must be a mapping, policyKey sequence/JSON string or RetryPolicy, "
        f"got {type(policy).__name__}"
    )


def _as_fallback(retry_policy: Any | None) -> RetryPolicy | None:
    if retry_policy is None:
        return None
    if not isinstance(retry_policy, RetryPolicy):
        raise TypeError(f"retry_policy must be a RetryPolicy, got {type(retry_policy).__name__}")
    return retry_policy


def _from_retry_policy(policy: Any) -> _Policy:
    if not isinstance(policy, RetryPolicy):
        raise TypeError(f"retry policy must be a RetryPolicy, got {type(policy).__name__}")
    codes = policy.retryable_codes if policy.retryable_codes is not None else RETRYABLE_CODES
    return _Policy(
        mode="normal",
        max_retries=policy.max_attempts - 1,
        retryable_codes=frozenset(codes),
        initial_delay_ms=policy.base_delay * 1000.0,
        max_delay_ms=policy.max_delay * 1000.0,
        jitter_ratio=policy.jitter,
    )


def _from_mapping(data: Mapping[str, Any], fallback: RetryPolicy | None) -> _Policy:
    backoff = data.get("backoff")
    if backoff is not None and not isinstance(backoff, Mapping):
        raise TypeError("retry policy backoff must be a mapping")
    mode = data.get("mode", "normal")
    if mode not in ("normal", "always"):
        raise ValueError(f'retry policy mode must be "normal" or "always", got {mode!r}')
    initial = _delay_value(
        data, backoff, ("initialDelayMs", "initial_delay_ms", "baseDelayMs", "base_delay_ms")
    )
    maximum = _delay_value(data, backoff, ("maxDelayMs", "max_delay_ms"))
    jitter = _delay_value(data, backoff, ("jitterRatio", "jitter_ratio", "jitter"))
    if initial is _MISSING:
        initial = fallback.base_delay * 1000.0 if fallback is not None else DEFAULT_INITIAL_DELAY_MS
    if maximum is _MISSING:
        maximum = fallback.max_delay * 1000.0 if fallback is not None else DEFAULT_MAX_DELAY_MS
    if jitter is _MISSING:
        jitter = fallback.jitter if fallback is not None else DEFAULT_JITTER_RATIO
    initial_ms = _number(initial, "backoff.initialDelayMs", minimum=0.0)
    max_ms = _number(maximum, "backoff.maxDelayMs", minimum=0.0, exclusive_minimum=True)
    jitter_ratio = _number(jitter, "backoff.jitterRatio", minimum=0.0, maximum=1.0)
    if initial_ms > max_ms:
        raise ValueError("backoff.initialDelayMs must be less than or equal to backoff.maxDelayMs")
    if mode == "always":
        return _Policy("always", None, frozenset(), initial_ms, max_ms, jitter_ratio)
    raw_codes = _policy_value(data, ("retryableCodes", "retryable_codes", "codes"))
    if raw_codes is _MISSING:
        raw_codes = fallback.retryable_codes if fallback is not None else None
    codes = RETRYABLE_CODES if raw_codes is None else _codes(raw_codes)
    raw_max = _policy_value(data, ("maxRetries", "max_retries"))
    if raw_max is _MISSING:
        raw_max = fallback.max_attempts - 1 if fallback is not None else DEFAULT_MAX_RETRIES
    max_retries = _integer(raw_max, "maxRetries")
    return _Policy("normal", max_retries, codes, initial_ms, max_ms, jitter_ratio)


def _from_policy_key(key: Any) -> _Policy:
    if isinstance(key, str):
        try:
            key = json.loads(key)
        except ValueError as exc:
            raise ValueError(f"retry policyKey is not valid JSON: {key[:120]!r}") from exc
    if not isinstance(key, Sequence) or isinstance(key, (str, bytes)):
        raise TypeError(f"retry policyKey must be a sequence, got {type(key).__name__}")
    items = list(key)
    if not items or not isinstance(items[0], str):
        raise ValueError('retry policyKey must start with "normal" or "always"')
    mode = items[0]
    if mode == "normal":
        if len(items) != 6:
            raise ValueError(
                "normal retry policyKey must be "
                "[mode, maxRetries, retryableCodes, initialDelayMs, maxDelayMs, jitterRatio]"
            )
        raw_max, raw_codes, initial, maximum, jitter = items[1:]
        codes = _codes(raw_codes)
        max_retries = _integer(raw_max, "maxRetries")
    elif mode == "always":
        if len(items) != 4:
            raise ValueError("always retry policyKey must be [mode, initialDelayMs, maxDelayMs, jitterRatio]")
        codes = frozenset()
        max_retries = None
        initial, maximum, jitter = items[1:]
    else:
        raise ValueError(f'retry policyKey mode must be "normal" or "always", got {mode!r}')
    initial_ms = _number(initial, "initialDelayMs", minimum=0.0)
    max_ms = _number(maximum, "maxDelayMs", minimum=0.0, exclusive_minimum=True)
    jitter_ratio = _number(jitter, "jitterRatio", minimum=0.0, maximum=1.0)
    if initial_ms > max_ms:
        raise ValueError("initialDelayMs must be less than or equal to maxDelayMs")
    if mode == "always":
        return _Policy("always", None, frozenset(), initial_ms, max_ms, jitter_ratio)
    assert max_retries is not None
    return _Policy("normal", max_retries, codes, initial_ms, max_ms, jitter_ratio)


def _policy_value(data: Mapping[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return _MISSING


def _delay_value(data: Mapping[str, Any], backoff: Any, names: Iterable[str]) -> Any:
    if backoff is not None:
        for name in names:
            if name in backoff:
                return backoff[name]
    return _policy_value(data, names)


def _policy_key(policy: _Policy) -> str:
    if policy.mode == "always":
        payload: list[Any] = [
            policy.mode,
            _number_out(policy.initial_delay_ms),
            _number_out(policy.max_delay_ms),
            _number_out(policy.jitter_ratio),
        ]
    else:
        payload = [
            policy.mode,
            policy.max_retries,
            sorted(policy.retryable_codes),
            _number_out(policy.initial_delay_ms),
            _number_out(policy.max_delay_ms),
            _number_out(policy.jitter_ratio),
        ]
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _delay_ms(
    policy: _Policy,
    retry: int,
    provider_retry_after_ms: Any,
    random_value: Callable[[], float],
) -> float | None:
    provider_delay = _optional_ms(provider_retry_after_ms)
    if provider_delay is not None and provider_delay > 0.0:
        if provider_delay > policy.max_delay_ms:
            if policy.mode == "normal":
                return None
        else:
            return provider_delay
    return _local_delay(policy, retry, random_value)


def _local_delay(policy: _Policy, retry: int, random_value: Callable[[], float]) -> float:
    exponent = min(retry - 1, MAX_EXPONENT)
    # 2.0 ** 1024 overflows a Python float; anything above 64 already exceeds the
    # largest DSH timer delay, so both branches cap at maxDelayMs.
    exponential = (
        policy.max_delay_ms
        if exponent > 64
        else min(policy.initial_delay_ms * 2.0**exponent, policy.max_delay_ms)
    )
    jitter = 1.0 - policy.jitter_ratio + 2.0 * policy.jitter_ratio * random_value()
    return min(exponential * jitter, policy.max_delay_ms)


def _stop(retry: int, retry_id: str | None, *, mode: str, policy_key: str) -> RetryDecision:
    return RetryDecision(
        action="stop",
        retry=retry,
        retry_id=retry_id,
        delay_ms=0.0,
        mode=mode,
        policy_key=policy_key,
        record=None,
        started_record=None,
    )


def _failure(failure: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(failure, Mapping):
        raise TypeError(f"failure must be a mapping, got {type(failure).__name__}")
    data = dict(failure)
    message = data.get("message")
    if not isinstance(message, str):
        raise TypeError("failure.message must be a string")
    code = data.get("code")
    if not isinstance(code, str) or not code:
        raise ValueError("failure.code must be a non-empty string")
    return data


def _provider(provider: str) -> str:
    if not isinstance(provider, str) or not provider:
        raise ValueError("provider must be a non-empty string")
    return provider


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    exclusive_minimum: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if minimum is not None:
        if exclusive_minimum and result <= minimum:
            raise ValueError(f"{name} must be > {minimum}, got {value!r}")
        if not exclusive_minimum and result < minimum:
            raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value!r}")
    return result


def _optional_ms(value: Any) -> float | None:
    """Provider Retry-After in milliseconds; non-numeric or non-finite values are ignored."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return result


def _random(rand: Callable[[], float] | None) -> Callable[[], float]:
    if rand is None:
        return random.random
    if not callable(rand):
        raise TypeError("rand must be callable")

    def draw() -> float:
        value = rand()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("rand must return a number")
        result = float(value)
        if not 0.0 <= result <= 1.0:
            raise ValueError(f"rand must return a value between 0 and 1, got {value!r}")
        return result

    return draw


def _codes(value: Any) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise TypeError(f"retryableCodes must be an iterable of strings, got {value!r}")
    codes: set[str] = set()
    for code in value:
        if not isinstance(code, str):
            raise TypeError(f"retryableCodes entries must be strings, got {code!r}")
        if not code:
            raise ValueError("retryableCodes entries must be non-empty")
        codes.add(code)
    if not codes:
        raise ValueError("retryableCodes must not be empty")
    return frozenset(codes)


def _previous(previous: Any | None) -> tuple[int, str | None]:
    if previous is None:
        return 0, None
    raw_retry = _previous_field(previous, "retry")
    retry = 0 if raw_retry is _MISSING else _integer(raw_retry, "previous.retry")
    raw_id = _previous_field(previous, "retryId", "retry_id")
    if raw_id is _MISSING or raw_id is None:
        return retry, None
    if not isinstance(raw_id, str) or not raw_id:
        raise ValueError("previous.retryId must be a non-empty string")
    return retry, raw_id


def _previous_field(value: Any, *names: str) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return _MISSING
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return _MISSING


def _number_out(value: float) -> float | int:
    """Render an integral float as int so records match DSH's JSON numbers."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
