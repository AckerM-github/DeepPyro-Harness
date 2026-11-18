"""Unit tests for the M2 budget tracker and frozen idempotency key."""

from __future__ import annotations

import hashlib
import math

import pytest

from deeppyro.agent.budget import (
    BudgetExceeded,
    BudgetKind,
    BudgetLimits,
    BudgetTracker,
    BudgetUsage,
    idempotency_key,
)
from deeppyro.core.llm import Usage

FROZEN_KEY = "idem:" + hashlib.sha256(b"session-a\ncall_0123456789abcdef\nsha256:abc").hexdigest()


def _usage(input_tokens: int = 0, output_tokens: int = 0) -> Usage:
    return Usage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _tracker(limits: BudgetLimits, clock: FakeClock | None = None) -> BudgetTracker:
    clock = clock or FakeClock()
    return BudgetTracker(limits, _started=clock(), _clock=clock)


def test_limits_validate_positive_values() -> None:
    assert BudgetLimits().empty is True
    assert BudgetLimits(max_steps=1).empty is False
    for kwargs in (
        {"max_steps": 0},
        {"max_steps": -1},
        {"max_steps": True},
        {"max_steps": 1.5},
        {"max_tokens": 0},
        {"max_wall_seconds": 0},
        {"max_wall_seconds": -1.0},
        {"max_wall_seconds": math.inf},
        {"max_wall_seconds": math.nan},
        {"max_cost": 0},
        {"max_cost": math.inf},
        {"max_cost": True},
    ):
        with pytest.raises(ValueError):
            BudgetLimits(**kwargs)


def test_limits_round_trip_and_closed_shape() -> None:
    limits = BudgetLimits(max_steps=3, max_tokens=None, max_wall_seconds=2.5, max_cost=0.25)
    assert BudgetLimits.from_dict(limits.to_dict()) == limits
    with pytest.raises(ValueError, match="unknown field"):
        BudgetLimits.from_dict({"max_steps": 1, "nope": 2})
    with pytest.raises(ValueError, match="must be an object"):
        BudgetLimits.from_dict([1])


def test_usage_accumulates_and_round_trips() -> None:
    usage = BudgetUsage()
    usage.add_usage(_usage(10, 5))
    usage.add_usage(_usage(1, 1))
    assert usage.input_tokens == 11
    assert usage.output_tokens == 6
    assert usage.total_tokens == 17
    usage.tool_calls = 2
    usage.wall_ms = 120
    usage.cost_usd = 0.5
    data = usage.to_dict()
    assert data == {
        "input_tokens": 11,
        "output_tokens": 6,
        "cost_usd": 0.5,
        "tool_calls": 2,
        "wall_ms": 120,
    }
    assert BudgetUsage.from_dict(data) == usage
    with pytest.raises(ValueError, match="unknown field"):
        BudgetUsage.from_dict({**data, "extra": 1})
    for broken in (
        {**data, "input_tokens": -1},
        {**data, "input_tokens": True},
        {**data, "cost_usd": -0.1},
        {**data, "cost_usd": "free"},
        {k: v for k, v in data.items() if k != "wall_ms"},
    ):
        with pytest.raises(ValueError):
            BudgetUsage.from_dict(broken)


def test_step_budget_raises_typed_overrun() -> None:
    tracker = _tracker(BudgetLimits(max_steps=2))
    tracker.start_step()
    tracker.start_step()
    with pytest.raises(BudgetExceeded) as caught:
        tracker.start_step()
    exc = caught.value
    assert exc.kind is BudgetKind.STEPS
    assert (exc.limit, exc.actual) == (2, 3)
    assert exc.turn_reason() == {"kind": "max_steps", "limit": 2}


def test_token_budget_uses_cumulative_totals() -> None:
    tracker = _tracker(BudgetLimits(max_tokens=10))
    tracker.start_step()
    tracker.record_model(_usage(4, 4))
    with pytest.raises(BudgetExceeded) as caught:
        tracker.record_model(_usage(1, 2))
    assert caught.value.kind is BudgetKind.TOKENS
    assert caught.value.turn_reason() == {"kind": "max_tokens", "limit": 10}


def test_wall_budget_uses_injected_clock() -> None:
    clock = FakeClock()
    tracker = _tracker(BudgetLimits(max_wall_seconds=2.0), clock)
    tracker.start_step()
    clock.now = 1.5
    tracker.start_step()
    clock.now = 2.5
    with pytest.raises(BudgetExceeded) as caught:
        tracker.check_wall()
    assert caught.value.kind is BudgetKind.WALL_SECONDS
    reason = caught.value.turn_reason()
    assert reason["kind"] == "error"
    assert reason["code"] == "budget_exceeded"
    assert "max_wall_seconds" in str(reason["message"])


def test_cost_budget_can_be_charged_from_model_or_externally() -> None:
    tracker = _tracker(BudgetLimits(max_cost=0.5))
    tracker.start_step()
    tracker.record_model(_usage(1, 1), cost_usd=0.3)
    with pytest.raises(BudgetExceeded) as caught:
        tracker.add_cost(0.3)
    assert caught.value.kind is BudgetKind.COST
    assert tracker.usage.cost_usd == pytest.approx(0.6)
    with pytest.raises(ValueError):
        tracker.add_cost(-1)
    with pytest.raises(ValueError):
        tracker.add_cost(True)


def test_tool_and_wall_usage_are_recorded() -> None:
    clock = FakeClock()
    tracker = _tracker(BudgetLimits(), clock)
    tracker.start_step()
    clock.now = 0.25
    tracker.start_step()
    tracker.record_tool()
    tracker.record_tool()
    tracker.record_model(_usage(2, 3), latency_ms=7)
    assert tracker.usage.tool_calls == 2
    assert tracker.usage.total_tokens == 5
    assert tracker.elapsed_seconds == 0.25
    assert tracker.usage.wall_ms >= 7


def test_idempotency_key_is_frozen() -> None:
    key = idempotency_key("session-a", "call_0123456789abcdef", "sha256:abc")
    assert key == FROZEN_KEY
    assert key.startswith("idem:")
    assert len(key) == len("idem:") + 64
    assert idempotency_key("session-b", "call_0123456789abcdef", "sha256:abc") != key
