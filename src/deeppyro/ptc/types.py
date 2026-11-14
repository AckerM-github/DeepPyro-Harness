"""PTC (programmatic tool calling) types and configuration.

The PTC transport executes one Python program per run_code call in a fresh
CPython subprocess.  A program can call the session's ordinary tools through
async bindings; the host proxies those calls back into the normal tool
registry.  Nothing in this package is an OS-level security boundary: the
subprocess only separates the program from the agent process, not from the
filesystem.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deeppyro.core.errors import ConfigError

PtcFailureKind = Literal[
    "exception",
    "timeout",
    "abort",
    "worker-exit",
    "invalid-output",
    "output-limit",
    "protocol",
    "sandbox-unavailable",
]

PtcMode = Literal["native", "ptc"]

PTC_MODES: frozenset[str] = frozenset({"native", "ptc"})

PTC_FAILURE_KINDS: frozenset[str] = frozenset(
    {
        "exception",
        "timeout",
        "abort",
        "worker-exit",
        "invalid-output",
        "output-limit",
        "protocol",
        "sandbox-unavailable",
    }
)

DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_MAX_TIMEOUT_MS = 600_000
DEFAULT_MAX_OUTPUT_BYTES = 67_108_864
DEFAULT_MAX_PENDING_CALLS = 128
DEFAULT_GRACE_MS = 3_000
DEFAULT_MAX_PARALLEL_SUB_CALLS = 10
DEFAULT_MAX_SUB_CALLS_PER_MINUTE = 1_024

SANDBOX_UNAVAILABLE_MESSAGE = "the Python PTC runtime has no OS-level sandbox policy"


@dataclass(frozen=True, slots=True)
class PtcFailure:
    """A structured program failure.

    kind is the transport-level failure category, not the Python exception
    class.  message is human readable and safe to place in the tool result.
    """

    kind: PtcFailureKind
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "message": self.message}


@dataclass(frozen=True, slots=True)
class PtcRunSpec:
    """One program invocation."""

    program: str
    cwd: Path
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    sandbox_mode: str = "workspace-write"


@dataclass(frozen=True, slots=True)
class PtcRunResult:
    """Result of one program invocation.

    value is the JSON-compatible value returned by the program, logs is the
    captured stdout/stderr stream (one entry per flushed chunk), and failure is
    set when the program did not complete successfully.
    """

    value: Any = None
    logs: tuple[str, ...] = ()
    failure: PtcFailure | None = None
    sandbox: dict[str, Any] = field(
        default_factory=lambda: {"mode": "workspace-write", "enforcement": "none", "denial": "none"}
    )

    @property
    def ok(self) -> bool:
        return self.failure is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "logs": list(self.logs),
            "failure": None if self.failure is None else self.failure.to_dict(),
            "sandbox": dict(self.sandbox),
        }


class PtcBindingError(Exception):
    """A structured error raised by an async binding inside the worker.

    The host sends this as an ok:false result frame.  The worker raises
    PtcToolCallError to the program with the same name/code/reason.
    """

    def __init__(self, name: str, code: str, reason: str = "") -> None:
        super().__init__(reason or name)
        self.name = name
        self.code = code
        self.reason = reason

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "code": self.code, "reason": self.reason}


def _require_table(value: Any, *, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        raise ConfigError(f"ptc: {name} must be a table")
    return value


def _pick(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _as_bool(value: Any, *, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"ptc: {name} must be a boolean")
    return value


def _positive_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"ptc: {name} must be a positive integer")
    return value


def _non_negative_int(value: Any, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"ptc: {name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class PtcConfig:
    """The [ptc] configuration table.

    enabled is a feature flag: when false the plugin still provides the runtime
    service (so it can be inspected), but run_code is not registered and the
    model never sees it.  Values accept DSH-style camelCase keys and snake_case
    aliases.
    """

    enabled: bool = False
    mode: PtcMode = "native"
    execution_mode: str = "per-call"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    max_timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_pending_calls: int = DEFAULT_MAX_PENDING_CALLS
    grace_ms: int = DEFAULT_GRACE_MS
    max_parallel_sub_calls: int = DEFAULT_MAX_PARALLEL_SUB_CALLS
    max_sub_calls_per_minute: int = DEFAULT_MAX_SUB_CALLS_PER_MINUTE

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | Any | None = None) -> PtcConfig:
        """Parse and validate a PTC configuration table.

        Unknown keys are rejected so a typo in config.toml is visible.  A
        requested timeoutMs larger than maxTimeoutMs is truncated to the maximum
        rather than rejected, matching the DSH timeout contract.
        """
        table = _require_table(mapping, name="configuration")
        known = {
            "enabled",
            "mode",
            "maxParallelSubCalls",
            "max_parallel_sub_calls",
            "maxSubCallsPerMinute",
            "max_sub_calls_per_minute",
            "executionMode",
            "execution_mode",
            "timeoutMs",
            "timeout_ms",
            "maxTimeoutMs",
            "max_timeout_ms",
            "maxOutputBytes",
            "max_output_bytes",
            "maxPendingCalls",
            "max_pending_calls",
            "graceMs",
            "grace_ms",
        }
        unexpected = set(table) - known
        if unexpected:
            raise ConfigError(f"ptc: unknown configuration key {min(unexpected)!r}")

        enabled_value = table.get("enabled", False)
        raw_enabled = _as_bool(enabled_value, name="enabled")
        explicit_mode = table.get("mode")
        if explicit_mode is None:
            # Backwards compatibility: the old boolean feature flag selected the
            # mode.  An explicit mode always wins over enabled.
            mode: PtcMode = "ptc" if raw_enabled else "native"
        else:
            if not isinstance(explicit_mode, str) or explicit_mode not in PTC_MODES:
                raise ConfigError("ptc: mode must be 'native' or 'ptc'")
            mode = explicit_mode  # type: ignore[assignment]
        enabled = mode == "ptc"
        execution_mode = _pick(table, "executionMode", "execution_mode") or "per-call"
        if not isinstance(execution_mode, str) or execution_mode != "per-call":
            raise ConfigError("ptc: executionMode must be 'per-call'")

        timeout_value = _pick(table, "timeoutMs", "timeout_ms")
        if timeout_value is None and "timeoutMs" not in table and "timeout_ms" not in table:
            timeout_value = DEFAULT_TIMEOUT_MS
        timeout_ms = _positive_int(timeout_value, name="timeoutMs")
        max_timeout_value = _pick(table, "maxTimeoutMs", "max_timeout_ms")
        if max_timeout_value is None and "maxTimeoutMs" not in table and "max_timeout_ms" not in table:
            max_timeout_value = DEFAULT_MAX_TIMEOUT_MS
        max_timeout_ms = _positive_int(max_timeout_value, name="maxTimeoutMs")
        timeout_ms = min(timeout_ms, max_timeout_ms)

        output_value = _pick(table, "maxOutputBytes", "max_output_bytes")
        if output_value is None and "maxOutputBytes" not in table and "max_output_bytes" not in table:
            output_value = DEFAULT_MAX_OUTPUT_BYTES
        max_output_bytes = _positive_int(output_value, name="maxOutputBytes")

        pending_value = _pick(table, "maxPendingCalls", "max_pending_calls")
        if pending_value is None and "maxPendingCalls" not in table and "max_pending_calls" not in table:
            pending_value = DEFAULT_MAX_PENDING_CALLS
        max_pending_calls = _positive_int(pending_value, name="maxPendingCalls")

        grace_value = _pick(table, "graceMs", "grace_ms")
        if grace_value is None and "graceMs" not in table and "grace_ms" not in table:
            grace_value = DEFAULT_GRACE_MS
        grace_ms = _non_negative_int(grace_value, name="graceMs")

        parallel_value = _pick(table, "maxParallelSubCalls", "max_parallel_sub_calls")
        if parallel_value is None and not (
            "maxParallelSubCalls" in table or "max_parallel_sub_calls" in table
        ):
            parallel_value = DEFAULT_MAX_PARALLEL_SUB_CALLS
        max_parallel_sub_calls = _positive_int(parallel_value, name="maxParallelSubCalls")

        rate_value = _pick(table, "maxSubCallsPerMinute", "max_sub_calls_per_minute")
        if rate_value is None and not (
            "maxSubCallsPerMinute" in table or "max_sub_calls_per_minute" in table
        ):
            rate_value = DEFAULT_MAX_SUB_CALLS_PER_MINUTE
        max_sub_calls_per_minute = _positive_int(rate_value, name="maxSubCallsPerMinute")
        return cls(
            enabled=enabled,
            mode=mode,
            execution_mode=execution_mode,
            timeout_ms=timeout_ms,
            max_timeout_ms=max_timeout_ms,
            max_output_bytes=max_output_bytes,
            max_pending_calls=max_pending_calls,
            grace_ms=grace_ms,
            max_parallel_sub_calls=max_parallel_sub_calls,
            max_sub_calls_per_minute=max_sub_calls_per_minute,
        )

    @property
    def timeout(self) -> dict[str, int]:
        """Return the DSH-shaped timeout descriptor."""
        return {"defaultMs": self.timeout_ms, "maxMs": self.max_timeout_ms}

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "executionMode": self.execution_mode,
            "timeoutMs": self.timeout_ms,
            "maxTimeoutMs": self.max_timeout_ms,
            "maxOutputBytes": self.max_output_bytes,
            "maxPendingCalls": self.max_pending_calls,
            "graceMs": self.grace_ms,
            "maxParallelSubCalls": self.max_parallel_sub_calls,
            "maxSubCallsPerMinute": self.max_sub_calls_per_minute,
        }


__all__ = [
    "DEFAULT_GRACE_MS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_PARALLEL_SUB_CALLS",
    "DEFAULT_MAX_PENDING_CALLS",
    "DEFAULT_MAX_SUB_CALLS_PER_MINUTE",
    "DEFAULT_MAX_TIMEOUT_MS",
    "DEFAULT_TIMEOUT_MS",
    "PTC_FAILURE_KINDS",
    "PTC_MODES",
    "SANDBOX_UNAVAILABLE_MESSAGE",
    "PtcBindingError",
    "PtcConfig",
    "PtcFailure",
    "PtcFailureKind",
    "PtcMode",
    "PtcRunResult",
    "PtcRunSpec",
]
