"""In-memory token/cost accounting service (API v3 skeleton).

The service mounts through the normal v3 path like every other plugin. It is
not part of the core bundle yet: agent turns do not report usage until the
context/usage lane wires request accounting in, so this module provides the
record types and the ledger that lane can build on.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, ClassVar

from deeppyro.api import PluginConfig, Service
from deeppyro.config import ModelProfile


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One accounted model response."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0


@dataclass(slots=True)
class UsageTotals:
    """Aggregate of UsageRecords, for one model or the whole service."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0

    def add(self, record: UsageRecord) -> None:
        """Fold one record into the total."""
        self.requests += 1
        self.input_tokens += record.input_tokens
        self.output_tokens += record.output_tokens
        self.cached_tokens += record.cached_tokens
        self.cost += record.cost

    def to_dict(self) -> dict[str, int | float]:
        """Return a plain mapping for status displays and tests."""
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cost": self.cost,
        }


def _non_negative(value: int, field: str) -> int:
    """Coerce a token count, rejecting negative values."""
    number = int(value)
    if number < 0:
        raise ValueError(f"{field} must not be negative")
    return number


class UsageService(Service):
    """In-memory token/cost ledger exposed as the "usage" service.

    Tokens are counted per model. Cost comes from the ModelProfile price list
    when a profile is supplied, and stays zero otherwise, so a host can account
    usage before provider pricing is configured.
    """

    provide = "usage"
    plugin_id = "usage"
    version = "0.1.0"
    inject: ClassVar[tuple[type[Any], ...]] = ()

    def __init__(self, ctx: Any, config: PluginConfig = None) -> None:
        super().__init__(ctx, config)
        self._records: list[UsageRecord] = []
        self._totals = UsageTotals()
        self._by_model: dict[str, UsageTotals] = {}

    def record(
        self,
        profile: ModelProfile | None = None,
        *,
        provider: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> UsageRecord:
        """Account one response and return the immutable record.

        Pass a ModelProfile to read provider, model and prices from it, or the
        bare provider/model strings when no profile is configured.
        """
        inputs = _non_negative(input_tokens, "input_tokens")
        outputs = _non_negative(output_tokens, "output_tokens")
        cached = _non_negative(cached_tokens, "cached_tokens")
        if profile is not None:
            provider = provider or profile.provider
            model = model or profile.id
            cost = profile.estimate_cost(input_tokens=inputs, output_tokens=outputs, cached_tokens=cached)
        else:
            cost = 0.0
        record = UsageRecord(
            provider=provider,
            model=model or "unknown",
            input_tokens=inputs,
            output_tokens=outputs,
            cached_tokens=cached,
            cost=cost,
        )
        self._records.append(record)
        self._totals.add(record)
        self._by_model.setdefault(record.model, UsageTotals()).add(record)
        return record

    def records(self) -> tuple[UsageRecord, ...]:
        """All records in accounting order."""
        return tuple(self._records)

    def totals(self) -> UsageTotals:
        """Aggregate over every record."""
        return self._totals

    def by_model(self) -> dict[str, UsageTotals]:
        """Per-model aggregates as independent copies."""
        return {model: replace(totals) for model, totals in self._by_model.items()}

    def summary(self) -> dict[str, Any]:
        """Plain report with totals and per-model aggregates."""
        return {
            "totals": self._totals.to_dict(),
            "models": {model: totals.to_dict() for model, totals in self._by_model.items()},
        }

    def reset(self) -> None:
        """Forget every record (session boundaries and tests)."""
        self._records.clear()
        self._totals = UsageTotals()
        self._by_model.clear()


__all__ = ["UsageRecord", "UsageService", "UsageTotals"]
