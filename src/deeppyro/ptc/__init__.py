"""Python programmatic tool calling (PTC).

Public surface:

* PtcRuntime / PtcService - the "ptc" service that executes run_code programs.
* RunCodeTool - the model-facing tool, registered only in [ptc].mode = "ptc".
* NestedDispatcher - the async tool bindings available to a program.
* PtcConfig and the structured result/failure types.
* PtcPlugin lives in deeppyro.builtin.ptc (kernel wiring).
"""

from __future__ import annotations

from .executor import NESTED_EXCLUDED_TOOLS, NestedDispatcher
from .runtime import WORKER_PATH, Binding, PtcRuntime, PtcService
from .tool import RUN_CODE_NAME, RunCodeTool
from .types import (
    DEFAULT_GRACE_MS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_PARALLEL_SUB_CALLS,
    DEFAULT_MAX_PENDING_CALLS,
    DEFAULT_MAX_SUB_CALLS_PER_MINUTE,
    DEFAULT_MAX_TIMEOUT_MS,
    DEFAULT_TIMEOUT_MS,
    PTC_FAILURE_KINDS,
    PTC_MODES,
    SANDBOX_UNAVAILABLE_MESSAGE,
    PtcBindingError,
    PtcConfig,
    PtcFailure,
    PtcFailureKind,
    PtcMode,
    PtcRunResult,
    PtcRunSpec,
)

__all__ = [
    "DEFAULT_GRACE_MS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_PARALLEL_SUB_CALLS",
    "DEFAULT_MAX_PENDING_CALLS",
    "DEFAULT_MAX_SUB_CALLS_PER_MINUTE",
    "DEFAULT_MAX_TIMEOUT_MS",
    "DEFAULT_TIMEOUT_MS",
    "NESTED_EXCLUDED_TOOLS",
    "PTC_FAILURE_KINDS",
    "PTC_MODES",
    "RUN_CODE_NAME",
    "SANDBOX_UNAVAILABLE_MESSAGE",
    "WORKER_PATH",
    "Binding",
    "NestedDispatcher",
    "PtcBindingError",
    "PtcConfig",
    "PtcFailure",
    "PtcFailureKind",
    "PtcMode",
    "PtcRunResult",
    "PtcRunSpec",
    "PtcRuntime",
    "PtcService",
    "RunCodeTool",
]
