"""Run budgets and typed overruns (M2 task 4).

A:class:`BudgetLimits` is a small immutable limit set. :class:`BudgetTracker`
accumulates the usage of one run and raises :class:`BudgetExceeded` the moment a
limit is crossed, so callers never have to re-check counters themselves.

The tracker is deliberately clock-injectable: budget enforcement is a
deterministic test surface, not a source of wall-clock flakiness.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from deeppyro.core.llm import Usage


class BudgetKind(StrEnum):
    """The budget dimension that was crossed."""

    STEPS = "max_steps"
    TOKENS = "max_tokens"
    WALL_SECONDS = "max_wall_seconds"
    COST = "max_cost"


class BudgetExceeded(Exception):
    """A run budget was crossed; the loop must stop and record the abort."""

    def __init__(self, kind: BudgetKind, limit: int | float, actual: int | float) -> None:
        self.kind = kind
        self.limit = limit
        self.actual = actual
        super().__init__(f"budget {kind.value} exceeded: limit {limit} actual {actual}")

    def turn_reason(self) -> dict[str, object]:
        """Return the native TurnReason object for this overrun."""
        if self.kind is BudgetKind.STEPS:
            return {"kind": "max_steps", "limit": int(self.limit)}
        if self.kind is BudgetKind.TOKENS:
            return {"kind": "max_tokens", "limit": int(self.limit)}
        return {"kind": "error", "message": str(self), "code": "budget_exceeded"}


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """Cumulative limits for one run; `None` means unlimited."""

    max_steps: int | None = None
    max_tokens: int | None = None
    max_wall_seconds: float | None = None
    max_cost: float | None = None

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive integer or None")
        for name in ("max_wall_seconds", "max_cost"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a positive number or None")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be a positive finite number or None")

    @property
    def empty(self) -> bool:
        """Whether no limit is configured."""
        return all(
            value is None for value in (self.max_steps, self.max_tokens, self.max_wall_seconds, self.max_cost)
        )

    def to_dict(self) -> dict[str, int | float | None]:
        """Serialize the limits for a checkpoint."""
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
            "max_wall_seconds": self.max_wall_seconds,
            "max_cost": self.max_cost,
        }

    @classmethod
    def from_dict(cls, raw: object) -> BudgetLimits:
        """Parse limits written by :meth:`to_dict`."""
        if not isinstance(raw, Mapping):
            raise ValueError("budget limits must be an object")
        unknown = set(raw) - {"max_steps", "max_tokens", "max_wall_seconds", "max_cost"}
        if unknown:
            raise ValueError(f"budget limits have an unknown field {sorted(unknown)[0]!r}")
        return cls(
            max_steps=raw.get("max_steps"),  # type: ignore[arg-type]
            max_tokens=raw.get("max_tokens"),  # type: ignore[arg-type]
            max_wall_seconds=raw.get("max_wall_seconds"),  # type: ignore[arg-type]
            max_cost=raw.get("max_cost"),  # type: ignore[arg-type]
        )


@dataclass(slots=True)
class BudgetUsage:
    """Cumulative run usage; also the payload of run.checkpoint budgets."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    wall_ms: int = 0

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens seen so far."""
        return self.input_tokens + self.output_tokens

    def add_usage(self, usage: Usage) -> None:
        """Add one model call usage report."""
        self.input_tokens += max(0, int(usage.input_tokens))
        self.output_tokens += max(0, int(usage.output_tokens))

    def to_dict(self) -> dict[str, int | float]:
        """Serialize to the closed run.checkpoint budgets object."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": float(self.cost_usd),
            "tool_calls": self.tool_calls,
            "wall_ms": self.wall_ms,
        }

    @classmethod
    def from_dict(cls, raw: object) -> BudgetUsage:
        """Parse a budgets object written by :meth:`to_dict`."""
        if not isinstance(raw, Mapping):
            raise ValueError("budgets must be an object")
        unknown = set(raw) - {"input_tokens", "output_tokens", "cost_usd", "tool_calls", "wall_ms"}
        if unknown:
            raise ValueError(f"budgets have an unknown field {sorted(unknown)[0]!r}")
        for name in ("input_tokens", "output_tokens", "tool_calls", "wall_ms"):
            value = raw.get(name)
            if type(value) is not int or value < 0:
                raise ValueError(f"budgets field {name!r} must be a non-negative integer")
        cost = raw.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise ValueError("budgets field 'cost_usd' must be a non-negative number")
        if not math.isfinite(float(cost)) or float(cost) < 0:
            raise ValueError("budgets field 'cost_usd' must be a non-negative number")
        return cls(
            input_tokens=int(raw["input_tokens"]),
            output_tokens=int(raw["output_tokens"]),
            cost_usd=float(cost),
            tool_calls=int(raw["tool_calls"]),
            wall_ms=int(raw["wall_ms"]),
        )


@dataclass(slots=True)
class BudgetTracker:
    """Accumulate one run's usage and enforce :class:`BudgetLimits`."""

    limits: BudgetLimits
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    steps: int = 0
    _started: float = field(default_factory=time.monotonic)
    _clock: Callable[[], float] = time.monotonic

    @property
    def elapsed_seconds(self) -> float:
        """Wall-clock seconds consumed by the run."""
        return max(0.0, self._clock() - self._started)

    def start_step(self) -> None:
        """Charge one model step and enforce the step and wall budgets."""
        self.usage.wall_ms = int(self.elapsed_seconds * 1000)
        self.check_wall()
        self.steps += 1
        if self.limits.max_steps is not None and self.steps > self.limits.max_steps:
            raise BudgetExceeded(BudgetKind.STEPS, self.limits.max_steps, self.steps)

    def record_model(self, usage: Usage, *, latency_ms: int = 0, cost_usd: float = 0.0) -> None:
        """Add one model call; raise when tokens, wall time or cost now overrun."""
        self.usage.add_usage(usage)
        self.usage.wall_ms += max(0, int(latency_ms))
        if cost_usd > 0:
            self.usage.cost_usd += float(cost_usd)
        self.check()
        if self.limits.max_tokens is not None and self.usage.total_tokens > self.limits.max_tokens:
            raise BudgetExceeded(BudgetKind.TOKENS, self.limits.max_tokens, self.usage.total_tokens)
        if self.limits.max_cost is not None and self.usage.cost_usd > self.limits.max_cost:
            raise BudgetExceeded(BudgetKind.COST, self.limits.max_cost, self.usage.cost_usd)

    def record_tool(self) -> None:
        """Count one executed tool call."""
        self.usage.tool_calls += 1

    def add_cost(self, cost_usd: float) -> None:
        """Add external cost (for example a pricing estimate) and enforce max_cost."""
        if isinstance(cost_usd, bool) or not isinstance(cost_usd, (int, float)):
            raise ValueError("cost_usd must be a number")
        if not math.isfinite(float(cost_usd)) or float(cost_usd) < 0:
            raise ValueError("cost_usd must be a finite non-negative number")
        self.usage.cost_usd += float(cost_usd)
        if self.limits.max_cost is not None and self.usage.cost_usd > self.limits.max_cost:
            raise BudgetExceeded(BudgetKind.COST, self.limits.max_cost, self.usage.cost_usd)

    def check_wall(self) -> None:
        """Raise when the wall-clock budget is exhausted."""
        if self.limits.max_wall_seconds is None:
            return
        elapsed = self.elapsed_seconds
        if elapsed > self.limits.max_wall_seconds:
            raise BudgetExceeded(BudgetKind.WALL_SECONDS, self.limits.max_wall_seconds, elapsed)

    def check(self) -> None:
        """Enforce the wall-clock budget (other limits are checked on record)."""
        self.check_wall()


def idempotency_key(session_id: str, call_id: str, tool_args_hash: str) -> str:
    """Return the frozen M2 tool idempotency key for one dispatched call.

    The format is defined by docs/native/schema.md section 10.3: `idem:` plus the
    lowercase SHA-256 hex digest of `session_id + "\n" + call_id + "\n" + args_hash`.
    """
    digest = hashlib.sha256(f"{session_id}\n{call_id}\n{tool_args_hash}".encode()).hexdigest()
    return "idem:" + digest


__all__ = [
    "BudgetExceeded",
    "BudgetKind",
    "BudgetLimits",
    "BudgetTracker",
    "BudgetUsage",
    "idempotency_key",
]
