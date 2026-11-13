"""Python programmatic tool calling (PTC).

Public surface:

* PtcRuntime / PtcService - the "ptc" service that executes run_code programs.
* RunCodeTool - the model-facing tool, registered only when [ptc].enabled is true.
* PtcConfig and the structured result/failure types.
* PtcPlugin lives in deeppyro.builtin.ptc (kernel wiring).
"""

from __future__ import annotations

from .runtime import WORKER_PATH, Binding, PtcRuntime, PtcService
from .tool import NESTED_EXCLUDED_TOOLS, RUN_CODE_NAME, RunCodeTool
from .types import (
    DEFAULT_GRACE_MS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_PENDING_CALLS,
    DEFAULT_MAX_TIMEOUT_MS,
    DEFAULT_TIMEOUT_MS,
    PTC_FAILURE_KINDS,
    SANDBOX_UNAVAILABLE_MESSAGE,
    PtcBindingError,
    PtcConfig,
    PtcFailure,
    PtcFailureKind,
    PtcRunResult,
    PtcRunSpec,
)

__all__ = [
    "DEFAULT_GRACE_MS",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_PENDING_CALLS",
    "DEFAULT_MAX_TIMEOUT_MS",
    "DEFAULT_TIMEOUT_MS",
    "NESTED_EXCLUDED_TOOLS",
    "PTC_FAILURE_KINDS",
    "RUN_CODE_NAME",
    "SANDBOX_UNAVAILABLE_MESSAGE",
    "WORKER_PATH",
    "Binding",
    "PtcBindingError",
    "PtcConfig",
    "PtcFailure",
    "PtcFailureKind",
    "PtcRunResult",
    "PtcRunSpec",
    "PtcRuntime",
    "PtcService",
    "RunCodeTool",
]
