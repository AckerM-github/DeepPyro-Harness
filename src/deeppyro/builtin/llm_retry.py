"""Builtin retry policy: provides "retryPolicy" and consumes agent/request-error.

The retry loop itself belongs to AgentSession._stream_step together with
agent.retry.decide_retry: the session appends llm/retry and llm/retry-started,
waits out the delay and re-runs the step.  This plugin only supplies two pieces:

* the policy object exposed as the "retryPolicy" service (a RetryPolicy in
  normal mode, or a policy mapping when [retry].mode = "always", because
  RetryPolicy cannot express the unbounded always mode), and
* an agent/request-error listener that answers {"kind": "retry"} when the
  payload carries a policy, letting the session perform the actual retry.

Keeping the listener separate from the policy service means the agent loop keeps
working unchanged when this plugin is disabled (no policy, no listener, no
retry records).

Configuration lives in the [retry] table of the top-level config:

    [retry]
    mode = "normal"          # or "always"
    maxRetries = 5
    initialDelayMs = 500
    maxDelayMs = 10000
    jitterRatio = 0.1
    retryableCodes = ["EMPTY_RESPONSE", "RATE_LIMIT", "SERVER", "TIMEOUT", "TRANSPORT"]

Missing keys fall back to the DSH defaults shown above; a missing table means
RetryPolicy() and therefore three attempts with the llm package defaults.  Invalid
values raise ConfigError while the plugin is constructed, so a bad [retry] table
fails the plugin at boot instead of silently disabling recovery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar

from deeppyro.agent.retry import (
    DEFAULT_INITIAL_DELAY_MS,
    DEFAULT_JITTER_RATIO,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MAX_RETRIES,
)
from deeppyro.api import Plugin
from deeppyro.core.errors import ConfigError
from deeppyro.hooks import AGENT_REQUEST_ERROR
from deeppyro.llm.retry import RetryPolicy

DEFAULT_MODE = "normal"
_RETRY_MODES = frozenset({"normal", "always"})
_MISSING = object()

#: The value registered under the "retryPolicy" service: a RetryPolicy for normal
#: mode (the session hands it to decide_retry) or a mapping for always mode.
RetryPolicyValue = RetryPolicy | dict[str, Any]


def build_retry_policy(raw: Any) -> RetryPolicyValue:
    """Build the retry policy value from the [retry] config table.

    raw is the extra top-level table as a mapping (or None when the user config
    omits [retry]).  Normal mode returns a RetryPolicy so that a programmatic
    service consumer sees the same object the llm package uses; always mode
    returns a mapping because decide_retry only preserves the unbounded mode for
    policy mappings.
    """
    if raw is None:
        return RetryPolicy()
    if not isinstance(raw, Mapping):
        raise ConfigError(f"[retry] must be a table, got {type(raw).__name__}")
    table = dict(raw)
    mode = _mode(table)
    initial = _number(
        table,
        ("initialDelayMs", "initial_delay_ms", "baseDelayMs", "base_delay_ms"),
        DEFAULT_INITIAL_DELAY_MS,
        "initialDelayMs",
        minimum=0.0,
    )
    maximum = _number(
        table,
        ("maxDelayMs", "max_delay_ms"),
        DEFAULT_MAX_DELAY_MS,
        "maxDelayMs",
        minimum=0.0,
        exclusive_minimum=True,
    )
    jitter = _number(
        table,
        ("jitterRatio", "jitter_ratio", "jitter"),
        DEFAULT_JITTER_RATIO,
        "jitterRatio",
        minimum=0.0,
        maximum=1.0,
    )
    if initial > maximum:
        raise ConfigError("[retry] initialDelayMs must be <= maxDelayMs")
    if mode == "always":
        return {
            "mode": "always",
            "initialDelayMs": initial,
            "maxDelayMs": maximum,
            "jitterRatio": jitter,
        }
    max_retries = _retry_count(table)
    codes = _codes(table)
    return RetryPolicy(
        max_attempts=max_retries + 1,
        base_delay=initial / 1000.0,
        max_delay=maximum / 1000.0,
        jitter=jitter,
        retryable_codes=codes,
    )


class LlmRetryPlugin(Plugin):
    """Provide the retry policy and turn request errors into retry decisions."""

    provide = "retryPolicy"
    plugin_id = "llm-retry"
    version = "0.1.0"
    description = "retry policy service plus agent/request-error retry consumer"
    inject: ClassVar[list[str]] = ["hooks", "config"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.service("config").value
        self._policy = build_retry_policy(getattr(cfg, "retry", None))
        self._hooks: Any | None = None

    @property
    def policy(self) -> RetryPolicyValue:
        """The value this plugin registers as the retryPolicy service."""
        return self._policy

    def setup(self, ctx: Any, config: Any) -> None:
        hooks = ctx.service("hooks")
        self._hooks = hooks
        ctx.provide("retryPolicy", self._policy)
        ctx.effect(
            lambda: hooks.on(AGENT_REQUEST_ERROR, self._on_request_error),
            label="agent-request-error",
        )

    async def _on_request_error(
        self, payload: dict[str, Any], next_step: Callable[[], Awaitable[Any]]
    ) -> Any:
        """Retry only when the session supplied a policy and is not cancelling."""
        if payload.get("retryPolicy") is None:
            return await next_step()
        signal = payload.get("signal")
        if _is_set(signal):
            return await next_step()
        return {"kind": "retry"}


def _mode(table: Mapping[str, Any]) -> str:
    raw = table.get("mode", DEFAULT_MODE)
    if not isinstance(raw, str) or raw.lower() not in _RETRY_MODES:
        raise ConfigError(f'[retry] mode must be "normal" or "always", got {raw!r}')
    return raw.lower()


def _retry_count(table: Mapping[str, Any]) -> int:
    raw = _first(table, ("maxRetries", "max_retries"))
    if raw is _MISSING:
        raw = DEFAULT_MAX_RETRIES
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"[retry] maxRetries must be an integer, got {raw!r}")
    if raw < 0:
        raise ConfigError(f"[retry] maxRetries must be >= 0, got {raw}")
    return raw


def _number(
    table: Mapping[str, Any],
    names: Sequence[str],
    default: float,
    field: str,
    *,
    minimum: float | None = None,
    exclusive_minimum: bool = False,
    maximum: float | None = None,
) -> float:
    raw = _first(table, names)
    if raw is _MISSING:
        return float(default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"[retry] {field} must be a number, got {raw!r}")
    value = float(raw)
    if minimum is not None:
        if exclusive_minimum and value <= minimum:
            raise ConfigError(f"[retry] {field} must be > {minimum:g}, got {value:g}")
        if not exclusive_minimum and value < minimum:
            raise ConfigError(f"[retry] {field} must be >= {minimum:g}, got {value:g}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"[retry] {field} must be <= {maximum:g}, got {value:g}")
    return value


def _codes(table: Mapping[str, Any]) -> frozenset[str] | None:
    raw = _first(table, ("retryableCodes", "retryable_codes", "codes"))
    if raw is _MISSING:
        return None
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ConfigError(f"[retry] retryableCodes must be a list of strings, got {raw!r}")
    codes: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item:
            raise ConfigError(f"[retry] retryableCodes must contain non-empty strings, got {item!r}")
        codes.add(item)
    return frozenset(codes)


def _first(table: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in table:
            return table[name]
    return _MISSING


def _is_set(signal: Any) -> bool:
    checker = getattr(signal, "is_set", None)
    if not callable(checker):
        return False
    return bool(checker())


__all__ = ["LlmRetryPlugin", "RetryPolicyValue", "build_retry_policy"]
