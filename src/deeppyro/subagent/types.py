"""Data types for the default subagent layer."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from deeppyro.subagent.errors import SubagentError

MAX_LIVE_RUNS = 4
DEFAULT_CONTEXT_WINDOW = 32768

#: Session record type carrying the provider usage of one finished child run.
USAGE_RECORD_TYPE = "subagent/usage"
USAGE_VERSION = 1
ASSISTANT_MESSAGE = "assistant/message"
LLM_RETRY = "llm/retry"

_USAGE_KEYS = {
    "inputtokens": "input_tokens",
    "outputtokens": "output_tokens",
    "cachedtokens": "cached_tokens",
    "latencyms": "latency_ms",
    "model": "model",
    "provider": "provider",
    "retries": "retries",
    "steps": "steps",
}


def _non_negative_int(value: object, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SubagentError(f"{subject} must be a non-negative integer")
    return value


def _text(value: object, subject: str) -> str:
    if not isinstance(value, str):
        raise SubagentError(f"{subject} must be a string")
    return value


def _optional_count(value: object) -> int:
    """Read a native usage counter, treating malformed data as absent."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


@dataclass(frozen=True, slots=True)
class SubagentUsage:
    """Provider-reported usage of one child run.

    Every counter comes from the child session's native records
    (assistant/message usage and llm/retry records); nothing here is estimated.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    provider: str = ""
    retries: int = 0
    steps: int = 0

    def __post_init__(self) -> None:
        _non_negative_int(self.input_tokens, "usage input_tokens")
        _non_negative_int(self.output_tokens, "usage output_tokens")
        _non_negative_int(self.cached_tokens, "usage cached_tokens")
        _non_negative_int(self.latency_ms, "usage latency_ms")
        _non_negative_int(self.retries, "usage retries")
        _non_negative_int(self.steps, "usage steps")
        _text(self.model, "usage model")
        _text(self.provider, "usage provider")

    @property
    def total_tokens(self) -> int:
        """Billed input plus output tokens."""
        return self.input_tokens + self.output_tokens

    def merge(self, other: SubagentUsage | None) -> SubagentUsage:
        """Add another usage measurement to this one."""
        if other is None:
            return self
        return SubagentUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
            latency_ms=self.latency_ms + other.latency_ms,
            model=self.model or other.model,
            provider=self.provider or other.provider,
            retries=self.retries + other.retries,
            steps=self.steps + other.steps,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-compatible (camelCase) native representation."""
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cachedTokens": self.cached_tokens,
            "latencyMs": self.latency_ms,
            "model": self.model,
            "provider": self.provider,
            "retries": self.retries,
            "steps": self.steps,
        }

    @classmethod
    def from_dict(cls, raw: object) -> SubagentUsage:
        """Rebuild usage from a native JSON object, rejecting unknown fields."""
        if not isinstance(raw, Mapping):
            raise SubagentError("usage must be a JSON object")
        resolved: dict[str, Any] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                raise SubagentError("usage field names must be strings")
            canonical = _USAGE_KEYS.get(key.replace("_", "").casefold())
            if canonical is None:
                raise SubagentError(f"usage has unknown field {key!r}")
            resolved[canonical] = value
        for key in ("model", "provider"):
            if key in resolved:
                resolved[key] = _text(resolved[key], f"usage {key}")
        return cls(
            input_tokens=_non_negative_int(resolved.get("input_tokens", 0), "usage input_tokens"),
            output_tokens=_non_negative_int(resolved.get("output_tokens", 0), "usage output_tokens"),
            cached_tokens=_non_negative_int(resolved.get("cached_tokens", 0), "usage cached_tokens"),
            latency_ms=_non_negative_int(resolved.get("latency_ms", 0), "usage latency_ms"),
            model=resolved.get("model", ""),
            provider=resolved.get("provider", ""),
            retries=_non_negative_int(resolved.get("retries", 0), "usage retries"),
            steps=_non_negative_int(resolved.get("steps", 0), "usage steps"),
        )


@runtime_checkable
class UsageSink(Protocol):
    """Optional consumer of child usage (structurally compatible with KI-018)."""

    def record(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> Any:
        """Record one provider usage measurement."""
        ...


def usage_from_records(
    records: Iterable[Any],
    *,
    model: str = "",
    provider: str = "",
    latency_ms: int = 0,
    baseline: SubagentUsage | None = None,
) -> SubagentUsage:
    """Fold provider usage out of persisted child session records.

    Only native facts are counted: the usage payload of every assistant/message
    record and the number of llm/retry records.  A baseline subtracts records a
    forked child inherited from its parent so one run never reports parent usage.
    """
    input_tokens = 0
    output_tokens = 0
    cached_tokens = 0
    retries = 0
    steps = 0
    for record in records:
        record_type: object = getattr(record, "type", None)
        data: object = getattr(record, "data", None)
        if isinstance(record, Mapping):
            raw_type = record.get("type")
            raw_data = record.get("data")
            if record_type is None:
                record_type = raw_type
            if data is None:
                data = raw_data
        if record_type == ASSISTANT_MESSAGE:
            steps += 1
            usage = data.get("usage") if isinstance(data, Mapping) else None
            if isinstance(usage, Mapping):
                input_tokens += _optional_count(_first(usage, "inputTokens", "input_tokens"))
                output_tokens += _optional_count(_first(usage, "outputTokens", "output_tokens"))
                cached_tokens += _optional_count(
                    _first(usage, "cacheReadTokens", "cachedTokens", "cached_tokens")
                )
        elif record_type == LLM_RETRY:
            retries += 1
    if baseline is not None:
        input_tokens = max(0, input_tokens - baseline.input_tokens)
        output_tokens = max(0, output_tokens - baseline.output_tokens)
        cached_tokens = max(0, cached_tokens - baseline.cached_tokens)
        retries = max(0, retries - baseline.retries)
        steps = max(0, steps - baseline.steps)
    return SubagentUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        latency_ms=_optional_count(latency_ms),
        model=model,
        provider=provider,
        retries=retries,
        steps=steps,
    )


def sum_usages(usages: Iterable[SubagentUsage | None]) -> SubagentUsage:
    """Aggregate several usage measurements (None entries are ignored)."""
    total = SubagentUsage()
    for usage in usages:
        total = total.merge(usage)
    return total


@dataclass(frozen=True, slots=True)
class SubagentActivation:
    """Handle returned as soon as a background child agent starts."""

    child_id: str
    session_id: str
    label: str
    provider: str
    model: str
    mode: str
    created_at: int


@dataclass(slots=True)
class SubagentResult:
    """Final reply of one child agent run."""

    child_id: str
    session_id: str
    label: str
    provider: str
    model: str
    text: str = ""
    is_error: bool = False
    error: str = ""
    mode: str = "spawn"
    usage: SubagentUsage | None = None


__all__ = [
    "ASSISTANT_MESSAGE",
    "DEFAULT_CONTEXT_WINDOW",
    "LLM_RETRY",
    "MAX_LIVE_RUNS",
    "USAGE_RECORD_TYPE",
    "USAGE_VERSION",
    "SubagentActivation",
    "SubagentResult",
    "SubagentUsage",
    "UsageSink",
    "sum_usages",
    "usage_from_records",
]
