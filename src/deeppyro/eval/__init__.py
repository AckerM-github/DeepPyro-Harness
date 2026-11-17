"""Provider-free evaluation, golden fixtures and native record/replay.

The eval package scores structured EvalOutcomes against EvalCases.  It ships a
recorded golden fixture and an offline native replay driver; neither imports a
provider nor opens a socket, so the default test suite stays offline.
"""

from __future__ import annotations

from deeppyro.eval.errors import EvalError, EvalFixtureError, EvalValidationError, ReplayError
from deeppyro.eval.fixture import (
    GoldenEntry,
    load_eval_cases,
    load_golden_entries,
    load_json_lines,
    load_recorded_outcomes,
)
from deeppyro.eval.replay import (
    ReplayCaseResult,
    ReplayCheck,
    ReplayReport,
    replay_fixtures,
    replay_recording,
)
from deeppyro.eval.runner import (
    AgentRunner,
    CallableAgentRunner,
    EvalRunner,
    ScriptedEvalRunner,
    check_outcome,
    run_recorded_golden,
)
from deeppyro.eval.types import (
    DEFAULT_MAX_STEPS,
    MAX_EVAL_STEPS,
    MEMORY_PROVENANCE_FIELDS,
    EvalBudget,
    EvalCase,
    EvalCheck,
    EvalExpectation,
    EvalOutcome,
    EvalReport,
    EvalResult,
    ExpectedToolCall,
    ToolInvocation,
)

__all__ = [
    "DEFAULT_MAX_STEPS",
    "MAX_EVAL_STEPS",
    "MEMORY_PROVENANCE_FIELDS",
    "AgentRunner",
    "CallableAgentRunner",
    "EvalBudget",
    "EvalCase",
    "EvalCheck",
    "EvalError",
    "EvalExpectation",
    "EvalFixtureError",
    "EvalOutcome",
    "EvalReport",
    "EvalResult",
    "EvalRunner",
    "EvalValidationError",
    "ExpectedToolCall",
    "GoldenEntry",
    "ReplayCaseResult",
    "ReplayCheck",
    "ReplayError",
    "ReplayReport",
    "ScriptedEvalRunner",
    "ToolInvocation",
    "check_outcome",
    "load_eval_cases",
    "load_golden_entries",
    "load_json_lines",
    "load_recorded_outcomes",
    "replay_fixtures",
    "replay_recording",
    "run_recorded_golden",
]
