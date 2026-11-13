"""Tests for the retry decision records."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent.retry import (
    DEFAULT_INITIAL_DELAY_MS,
    DEFAULT_JITTER_RATIO,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MAX_RETRIES,
    RetryDecision,
    decide_retry,
)
from deeppyro.llm.retry import RETRYABLE_CODES, RetryPolicy

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
RETRY_FIXTURE = FIXTURES / "records" / "by-type" / "llm_retry.jsonl"
STARTED_FIXTURE = FIXTURES / "records" / "by-type" / "llm_retry_started.jsonl"

NORMAL_POLICY: dict[str, Any] = {
    "mode": "normal",
    "maxRetries": 5,
    "retryableCodes": ["EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"],
    "backoff": {"initialDelayMs": 500, "maxDelayMs": 10000, "jitterRatio": 0.1},
}
NORMAL_POLICY_KEY = (
    '["normal",5,["EMPTY_RESPONSE","RATE_LIMIT","SERVER","TIMEOUT","TRANSPORT"],500,10000,0.1]'
)
ALWAYS_POLICY: dict[str, Any] = {
    "mode": "always",
    "backoff": {"initialDelayMs": 500, "maxDelayMs": 10000, "jitterRatio": 0.1},
}
FAILURE: dict[str, Any] = {
    "message": '502 "acp-proxy failure: upstream-reset UND_ERR_SOCKET 127.0.0.1:8000"',
    "code": "SERVER",
}


def fixture_data(path: Path) -> dict[str, Any]:
    line = path.read_text(encoding="utf-8").splitlines()[0]
    return json.loads(line)["data"]


def decide(**overrides: Any) -> RetryDecision:
    arguments: dict[str, Any] = {
        "failure": FAILURE,
        "policy": NORMAL_POLICY,
        "previous": None,
        "provider": "deepseek-v4-pro-qwen35-9b",
        "turn": 3,
        "step": 1,
    }
    arguments.update(overrides)
    return decide_retry(**arguments)


def test_no_policy_means_stop() -> None:
    decision = decide(policy=None)
    assert decision.action == "stop"
    assert decision.should_retry is False
    assert decision.retry == 0
    assert decision.retry_id is None
    assert decision.delay_ms == 0.0
    assert decision.mode == ""
    assert decision.policy_key == ""
    assert decision.record is None
    assert decision.started_record is None


def test_non_retryable_code_stops() -> None:
    decision = decide(failure={"message": "bad key", "code": "AUTH"}, previous={"retry": 1, "retryId": "rid"})
    assert decision.action == "stop"
    assert decision.retry == 1
    assert decision.retry_id == "rid"
    assert decision.mode == "normal"
    assert decision.policy_key == NORMAL_POLICY_KEY


def test_retry_budget_exhausted_stops() -> None:
    decision = decide(previous={"retry": 5, "retryId": "rid"})
    assert decision.action == "stop"
    assert decision.retry == 5
    assert decision.retry_id == "rid"
    assert decision.record is None
    assert decision.started_record is None


def test_new_retry_id_is_a_uuid_when_no_previous_retry() -> None:
    decision = decide(rand=lambda: 0.5)
    assert decision.action == "retry"
    assert decision.retry == 1
    assert decision.retry_id is not None
    assert uuid.UUID(decision.retry_id).version == 4


def test_previous_retry_id_is_reused() -> None:
    decision = decide(previous={"retry": 2, "retryId": "chain-id"}, rand=lambda: 0.5)
    assert decision.action == "retry"
    assert decision.retry == 3
    assert decision.retry_id == "chain-id"
    assert decision.record is not None
    assert decision.record["retryId"] == "chain-id"
    assert decision.started_record == {
        "retryId": "chain-id",
        "turn": 3,
        "step": 1,
        "retry": 3,
    }


def test_previous_entry_may_be_an_object() -> None:
    previous = SimpleNamespace(retry=1, retryId="object-id")
    decision = decide(previous=previous, rand=lambda: 0.5)
    assert decision.retry == 2
    assert decision.retry_id == "object-id"


def test_previous_entry_with_snake_case_id() -> None:
    decision = decide(previous={"retry": 1, "retry_id": "snake-id"}, rand=lambda: 0.5)
    assert decision.retry_id == "snake-id"


def test_empty_previous_entry_starts_the_chain() -> None:
    decision = decide(previous={}, rand=lambda: 0.5)
    assert decision.retry == 1
    assert decision.retry_id is not None


def test_normal_record_matches_the_fixture_shape() -> None:
    fixture = fixture_data(RETRY_FIXTURE)
    started = fixture_data(STARTED_FIXTURE)
    decision = decide(
        failure=fixture["failure"],
        policy=fixture["policyKey"],
        previous={"retry": 0, "retryId": fixture["retryId"]},
        provider=fixture["provider"],
        turn=fixture["turn"],
        step=fixture["step"],
        rand=lambda: 0.5,
    )
    assert decision.action == "retry"
    assert decision.policy_key == fixture["policyKey"]
    assert decision.started_record == started
    record = decision.record
    assert record is not None
    assert list(record) == [
        "retryId",
        "turn",
        "step",
        "provider",
        "mode",
        "policyKey",
        "retry",
        "maxRetries",
        "delayMs",
        "failure",
    ]
    assert record["mode"] == "normal"
    assert record["maxRetries"] == 5
    assert record["retry"] == 1
    assert record["delayMs"] == 500
    assert isinstance(record["delayMs"], int)
    assert record["failure"] == fixture["failure"]


def test_fixture_jitter_is_reproduced() -> None:
    fixture = fixture_data(RETRY_FIXTURE)
    # The fixture delay is 500 * (0.9 + 0.2 * rand), so invert it to replay the draw.
    rand_value = (fixture["delayMs"] / DEFAULT_INITIAL_DELAY_MS - 0.9) / 0.2
    decision = decide(
        failure=fixture["failure"],
        policy=fixture["policyKey"],
        previous={"retry": 0, "retryId": fixture["retryId"]},
        provider=fixture["provider"],
        turn=fixture["turn"],
        step=fixture["step"],
        rand=lambda: rand_value,
    )
    assert decision.delay_ms == pytest.approx(fixture["delayMs"])
    assert decision.record is not None
    assert decision.record == {**fixture, "delayMs": pytest.approx(fixture["delayMs"])}


def test_delay_doubles_with_the_retry_count() -> None:
    first = decide(previous={"retry": 0}, rand=lambda: 1.0)
    second = decide(previous={"retry": 1}, rand=lambda: 1.0)
    third = decide(previous={"retry": 2}, rand=lambda: 1.0)
    assert [first.delay_ms, second.delay_ms, third.delay_ms] == [550.0, 1100.0, 2200.0]


def test_delay_uses_lower_jitter_bound() -> None:
    decision = decide(previous={"retry": 0}, rand=lambda: 0.0)
    assert decision.delay_ms == pytest.approx(450.0)


def test_delay_is_capped_at_max_delay() -> None:
    policy = {
        "mode": "normal",
        "maxRetries": 5,
        "retryableCodes": ["SERVER"],
        "backoff": {"initialDelayMs": 500, "maxDelayMs": 700, "jitterRatio": 0.1},
    }
    first = decide(policy=policy, previous={"retry": 0}, rand=lambda: 1.0)
    second = decide(policy=policy, previous={"retry": 1}, rand=lambda: 1.0)
    assert first.delay_ms == 550.0
    assert second.delay_ms == 700.0


def test_provider_retry_after_wins_over_local_backoff() -> None:
    decision = decide(
        failure={**FAILURE, "providerRetryAfterMs": 7000},
        previous={"retry": 0},
        rand=lambda: 1.0,
    )
    assert decision.delay_ms == 7000.0


def test_provider_retry_after_above_cap_stops_normal_mode() -> None:
    decision = decide(failure={**FAILURE, "providerRetryAfterMs": 20000}, previous={"retry": 0})
    assert decision.action == "stop"
    assert decision.mode == "normal"


@pytest.mark.parametrize("retry_after", [None, "1500", float("nan"), 0, -25, True])
def test_unusable_provider_retry_after_falls_back_to_backoff(retry_after: Any) -> None:
    failure = dict(FAILURE) if retry_after is None else {**FAILURE, "providerRetryAfterMs": retry_after}
    decision = decide(failure=failure, previous={"retry": 0}, rand=lambda: 0.5)
    assert decision.delay_ms == 500.0


def test_always_mode_retries_anything_without_a_budget() -> None:
    decision = decide(
        failure={"message": "bad key", "code": "AUTH"},
        policy=ALWAYS_POLICY,
        previous={"retry": 99, "retryId": "chain"},
        rand=lambda: 0.5,
    )
    assert decision.action == "retry"
    assert decision.retry == 100
    assert decision.retry_id == "chain"
    assert decision.mode == "always"
    assert decision.policy_key == '["always",500,10000,0.1]'
    assert decision.delay_ms == DEFAULT_MAX_DELAY_MS
    assert decision.record is not None
    assert "maxRetries" not in decision.record
    assert decision.record == {
        "retryId": "chain",
        "turn": 3,
        "step": 1,
        "provider": "deepseek-v4-pro-qwen35-9b",
        "mode": "always",
        "policyKey": '["always",500,10000,0.1]',
        "retry": 100,
        "delayMs": 10000,
        "failure": {"message": "bad key", "code": "AUTH"},
    }


def test_always_mode_ignores_provider_retry_after_above_cap() -> None:
    decision = decide(
        failure={**FAILURE, "providerRetryAfterMs": 20000},
        policy=ALWAYS_POLICY,
        previous={"retry": 0, "retryId": "chain"},
        rand=lambda: 0.5,
    )
    assert decision.action == "retry"
    assert decision.delay_ms == 500.0


def test_retry_policy_object_is_accepted() -> None:
    fixture = fixture_data(RETRY_FIXTURE)
    policy = RetryPolicy(max_attempts=6, base_delay=0.5, max_delay=10.0, jitter=0.1)
    decision = decide(policy=policy, rand=lambda: 0.5)
    assert decision.policy_key == fixture["policyKey"]
    assert decision.retry == 1
    assert decision.delay_ms == 500.0
    assert "SERVER" in RETRYABLE_CODES


def test_retry_policy_object_is_the_fallback_policy() -> None:
    fixture = fixture_data(RETRY_FIXTURE)
    policy = RetryPolicy.from_policy(fixture["policyKey"])
    decision = decide(policy=None, retry_policy=policy, rand=lambda: 0.5)
    assert decision.action == "retry"
    assert decision.policy_key == fixture["policyKey"]
    assert decision.delay_ms == 500.0


def test_partial_mapping_inherits_codes_and_backoff() -> None:
    fallback = RetryPolicy(
        max_attempts=3,
        base_delay=1.0,
        max_delay=5.0,
        jitter=0.0,
        retryable_codes=frozenset({"SERVER"}),
    )
    decision = decide(policy={"mode": "normal"}, retry_policy=fallback)
    assert decision.action == "retry"
    assert decision.mode == "normal"
    assert decision.policy_key == '["normal",2,["SERVER"],1000,5000,0]'
    assert decision.delay_ms == 1000.0
    assert decision.record is not None
    assert decision.record["maxRetries"] == 2


def test_policy_key_sequence_is_accepted() -> None:
    decision = decide(policy=["normal", 2, ["SERVER"], 100, 1000, 0.0], rand=lambda: 1.0)
    assert decision.action == "retry"
    assert decision.policy_key == '["normal",2,["SERVER"],100,1000,0]'
    assert decision.delay_ms == 100.0
    assert decision.record is not None
    assert decision.record["maxRetries"] == 2


def test_flat_mapping_aliases_are_accepted() -> None:
    policy = {
        "mode": "normal",
        "maxRetries": 1,
        "codes": ["TIMEOUT", "SERVER"],
        "baseDelayMs": 100,
        "maxDelayMs": 1000,
        "jitter": 0,
    }
    decision = decide(policy=policy, rand=lambda: 1.0)
    assert decision.action == "retry"
    assert decision.policy_key == '["normal",1,["SERVER","TIMEOUT"],100,1000,0]'
    assert decision.delay_ms == 100.0


def test_policy_key_codes_are_sorted() -> None:
    policy = {
        "mode": "normal",
        "maxRetries": 5,
        "retryableCodes": ["TIMEOUT", "SERVER"],
        "backoff": {"initialDelayMs": 500, "maxDelayMs": 10000, "jitterRatio": 0.1},
    }
    decision = decide(policy=policy, rand=lambda: 0.5)
    assert decision.policy_key == '["normal",5,["SERVER","TIMEOUT"],500,10000,0.1]'


def test_defaults_match_dsh() -> None:
    assert (DEFAULT_MAX_RETRIES, DEFAULT_INITIAL_DELAY_MS, DEFAULT_MAX_DELAY_MS, DEFAULT_JITTER_RATIO) == (
        5,
        500.0,
        10000.0,
        0.1,
    )


@pytest.mark.parametrize(
    "policy",
    [
        pytest.param("not json", id="bad-json"),
        pytest.param({"mode": "sometimes"}, id="bad-mode"),
        pytest.param(["normal", 5], id="short-sequence"),
        pytest.param(["always", 500, 10000], id="short-always"),
        pytest.param({"mode": "normal", "maxRetries": -1}, id="negative-budget"),
        pytest.param({"mode": "normal", "retryableCodes": []}, id="empty-codes"),
        pytest.param({"mode": "normal", "backoff": {"maxDelayMs": 0}}, id="zero-max-delay"),
        pytest.param(
            {"mode": "normal", "backoff": {"initialDelayMs": 2000, "maxDelayMs": 1000}}, id="inverted"
        ),
        pytest.param({"mode": "normal", "backoff": {"jitterRatio": 1.5}}, id="jitter-range"),
    ],
)
def test_invalid_policies_are_rejected(policy: Any) -> None:
    with pytest.raises(ValueError):
        decide(policy=policy)


def test_unsupported_policy_type_is_rejected() -> None:
    with pytest.raises(TypeError):
        decide(policy=123)
    with pytest.raises(TypeError):
        decide(policy=None, retry_policy=object())


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ({"code": "SERVER"}, TypeError),
        ({"message": "x", "code": ""}, ValueError),
        ({"message": "x", "code": 5}, ValueError),
        ("not a failure", TypeError),
    ],
)
def test_invalid_failures_are_rejected(failure: Any, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        decide(failure=failure)


def test_invalid_envelope_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        decide(provider="")
    with pytest.raises(TypeError):
        decide(turn=True)
    with pytest.raises(ValueError):
        decide(step=-1)
    with pytest.raises(TypeError):
        decide(previous={"retry": "1"})
    with pytest.raises(ValueError):
        decide(previous={"retry": 1, "retryId": ""})


def test_invalid_rand_is_rejected() -> None:
    with pytest.raises(TypeError):
        decide(rand="random")
    with pytest.raises(ValueError):
        decide(rand=lambda: 1.5)
    with pytest.raises(TypeError):
        decide(rand=lambda: "half")


def test_stop_decision_never_builds_records() -> None:
    decision = decide(failure={"message": "x", "code": "AUTH"})
    assert decision.record is None
    assert decision.started_record is None
    assert decision.should_retry is False


def test_decision_is_frozen() -> None:
    decision = decide(rand=lambda: 0.5)
    with pytest.raises(AttributeError):
        decision.retry = 9  # type: ignore[misc]
