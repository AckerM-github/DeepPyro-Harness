"""Offline evaluation data shapes: cases, expectations, outcomes and results.

The evaluation layer is deliberately provider-free.  An AgentRunner turns one
EvalCase into an EvalOutcome; EvalRunner compares that outcome against the
expectations and emits deterministic EvalResults.  Golden fixtures pair every
case with a recorded outcome so CI can replay the suite without calling a model.

Results are deterministic by construction: case ids are stable lowercase slugs,
checks are emitted in a fixed order and EvalReport always sorts its results by
case id.  Timing is deliberately not part of the serialized result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from deeppyro.eval.errors import EvalValidationError
from deeppyro.memory.backend import PROVENANCE_FIELDS, MemoryRecord
from deeppyro.memory.errors import MemoryValidationError

#: Stable eval case ids: lowercase slug so results sort predictably.
CASE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

#: Default per-case step budget when a fixture does not specify one.
DEFAULT_MAX_STEPS = 8

#: Upper bound accepted for a per-case step budget, mirroring TaskBudget.
MAX_EVAL_STEPS = 4096

#: Provenance fields an expectation may require on every memory record.
MEMORY_PROVENANCE_FIELDS = PROVENANCE_FIELDS


def _reject_unknown(raw: dict[str, object], allowed: set[str], subject: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise EvalValidationError(f"{subject} has unknown field {unknown[0]!r}")


def _require_field(raw: dict[str, object], name: str, subject: str) -> object:
    if name not in raw:
        raise EvalValidationError(f"{subject} lacks required field {name!r}")
    return raw[name]


def _mapping(value: object, subject: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise EvalValidationError(f"{subject} must be a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise EvalValidationError(f"{subject} keys must be strings")
        result[key] = item
    return result


def _text(value: object, subject: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise EvalValidationError(f"{subject} must be a string")
    if not allow_empty and not value.strip():
        raise EvalValidationError(f"{subject} must be a non-empty string")
    return value


def _optional_text(value: object, subject: str) -> str | None:
    if value is None:
        return None
    return _text(value, subject)


def _count(value: object, subject: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise EvalValidationError(f"{subject} must be an integer >= {minimum}")
    return value


def _optional_count(value: object, subject: str) -> int | None:
    if value is None:
        return None
    return _count(value, subject)


def _positive_number(value: object, subject: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise EvalValidationError(f"{subject} must be a positive number")
    return float(value)


def _optional_bool(value: object, subject: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise EvalValidationError(f"{subject} must be a boolean or null")
    return value


def _bool(value: object, subject: str) -> bool:
    if not isinstance(value, bool):
        raise EvalValidationError(f"{subject} must be a boolean")
    return value


def _string_tuple(value: object, subject: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise EvalValidationError(f"{subject} must be a sequence of strings, not a string")
    if not isinstance(value, (list, tuple)):
        raise EvalValidationError(f"{subject} must be a sequence of strings")
    result: list[str] = []
    for item in value:
        text = _text(item, f"{subject} entry")
        if text not in result:
            result.append(text)
    return tuple(result)


def _json_mapping(value: object, subject: str) -> dict[str, object]:
    result = _mapping(value, subject)
    try:
        json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise EvalValidationError(f"{subject} must be JSON-serializable") from exc
    return result


def _memory_records(value: object, subject: str) -> tuple[MemoryRecord, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise EvalValidationError(f"{subject} must be a sequence of memory records")
    records: list[MemoryRecord] = []
    for index, item in enumerate(value):
        try:
            records.append(MemoryRecord.from_dict(item))
        except MemoryValidationError as exc:
            raise EvalValidationError(f"{subject}[{index}] is not a memory record: {exc}") from exc
    return tuple(records)


@dataclass(frozen=True, slots=True)
class EvalBudget:
    """Per-case budget used for checking and for the runner timeout."""

    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int | None = None
    max_seconds: float | None = None

    def __post_init__(self) -> None:
        steps = _count(self.max_steps, "eval budget max_steps", minimum=1)
        if steps > MAX_EVAL_STEPS:
            raise EvalValidationError(f"eval budget max_steps may not exceed {MAX_EVAL_STEPS}")
        if self.max_tokens is not None:
            _count(self.max_tokens, "eval budget max_tokens", minimum=1)
        if self.max_seconds is not None:
            _positive_number(self.max_seconds, "eval budget max_seconds")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping, omitting absent limits."""
        data: dict[str, object] = {"max_steps": self.max_steps}
        if self.max_tokens is not None:
            data["max_tokens"] = self.max_tokens
        if self.max_seconds is not None:
            data["max_seconds"] = self.max_seconds
        return data

    @classmethod
    def from_dict(cls, raw: object) -> EvalBudget:
        """Parse one budget mapping, refusing unknown fields."""
        data = _mapping(raw, "eval budget")
        _reject_unknown(data, {"max_steps", "max_tokens", "max_seconds"}, "eval budget")
        return cls(
            max_steps=_count(data.get("max_steps", DEFAULT_MAX_STEPS), "eval budget max_steps", minimum=1),
            max_tokens=_optional_count(data.get("max_tokens"), "eval budget max_tokens"),
            max_seconds=(
                None
                if data.get("max_seconds") is None
                else _positive_number(data.get("max_seconds"), "eval budget max_seconds")
            ),
        )


@dataclass(frozen=True, slots=True)
class ExpectedToolCall:
    """One tool invocation the agent is expected to make.

    arguments is a subset match: every expected key must equal the observed
    argument value, while extra observed keys are tolerated.
    """

    name: str
    arguments: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.name, "expected tool call name")
        object.__setattr__(
            self, "arguments", _json_mapping(self.arguments or {}, "expected tool call arguments")
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping."""
        return {"name": self.name, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, raw: object) -> ExpectedToolCall:
        """Parse one expected tool call, refusing unknown fields."""
        data = _mapping(raw, "expected tool call")
        _reject_unknown(data, {"name", "arguments"}, "expected tool call")
        name = _text(_require_field(data, "name", "expected tool call"), "expected tool call name")
        return cls(
            name=name, arguments=_json_mapping(data.get("arguments", {}), "expected tool call arguments")
        )


@dataclass(frozen=True, slots=True)
class EvalExpectation:
    """What a passing outcome must look like.

    Every dimension is optional: unset booleans are not checked and empty tuples
    are trivially satisfied.  A case fails when any checked dimension misses.
    """

    tool_calls: tuple[ExpectedToolCall, ...] = ()
    final_text_contains: tuple[str, ...] = ()
    memory_contains: tuple[str, ...] = ()
    memory_provenance: tuple[str, ...] = ()
    error_contains: tuple[str, ...] = ()
    refused: bool | None = None
    budget_exceeded: bool | None = None
    recovered: bool | None = None
    allows_error: bool = False

    def __post_init__(self) -> None:
        if not all(isinstance(item, ExpectedToolCall) for item in self.tool_calls):
            raise EvalValidationError("eval expectation tool_calls must be ExpectedToolCall values")
        _string_tuple(self.final_text_contains, "eval expectation final_text_contains")
        _string_tuple(self.memory_contains, "eval expectation memory_contains")
        _string_tuple(self.error_contains, "eval expectation error_contains")
        fields = _string_tuple(self.memory_provenance, "eval expectation memory_provenance")
        unknown = sorted(set(fields) - set(MEMORY_PROVENANCE_FIELDS))
        if unknown:
            raise EvalValidationError(f"eval expectation has unknown provenance field {unknown[0]!r}")
        for name in ("refused", "budget_exceeded", "recovered"):
            _optional_bool(getattr(self, name), f"eval expectation {name}")
        _bool(self.allows_error, "eval expectation allows_error")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping with a stable key order."""
        return {
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "final_text_contains": list(self.final_text_contains),
            "memory_contains": list(self.memory_contains),
            "memory_provenance": list(self.memory_provenance),
            "error_contains": list(self.error_contains),
            "refused": self.refused,
            "budget_exceeded": self.budget_exceeded,
            "recovered": self.recovered,
            "allows_error": self.allows_error,
        }

    @classmethod
    def from_dict(cls, raw: object) -> EvalExpectation:
        """Parse one expectation mapping, refusing unknown fields."""
        data = _mapping(raw, "eval expectation")
        allowed = {
            "tool_calls",
            "final_text_contains",
            "memory_contains",
            "memory_provenance",
            "error_contains",
            "refused",
            "budget_exceeded",
            "recovered",
            "allows_error",
        }
        _reject_unknown(data, allowed, "eval expectation")
        raw_calls = data.get("tool_calls", [])
        if not isinstance(raw_calls, (list, tuple)):
            raise EvalValidationError("eval expectation tool_calls must be a list")
        return cls(
            tool_calls=tuple(ExpectedToolCall.from_dict(item) for item in raw_calls),
            final_text_contains=_string_tuple(
                data.get("final_text_contains"), "eval expectation final_text_contains"
            ),
            memory_contains=_string_tuple(data.get("memory_contains"), "eval expectation memory_contains"),
            memory_provenance=_string_tuple(
                data.get("memory_provenance"), "eval expectation memory_provenance"
            ),
            error_contains=_string_tuple(data.get("error_contains"), "eval expectation error_contains"),
            refused=_optional_bool(data.get("refused"), "eval expectation refused"),
            budget_exceeded=_optional_bool(data.get("budget_exceeded"), "eval expectation budget_exceeded"),
            recovered=_optional_bool(data.get("recovered"), "eval expectation recovered"),
            allows_error=_bool(data.get("allows_error", False), "eval expectation allows_error"),
        )


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One recorded, reusable evaluation task with stable id and expectations."""

    id: str
    prompt: str
    budget: EvalBudget = field(default_factory=EvalBudget)
    expect: EvalExpectation = field(default_factory=EvalExpectation)
    tags: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        case_id = _text(self.id, "eval case id")
        if not CASE_ID_RE.fullmatch(case_id):
            raise EvalValidationError("eval case id must be a lowercase slug of 3-64 characters")
        _text(self.prompt, "eval case prompt")
        if not isinstance(self.budget, EvalBudget):
            raise EvalValidationError("eval case budget must be an EvalBudget")
        if not isinstance(self.expect, EvalExpectation):
            raise EvalValidationError("eval case expect must be an EvalExpectation")
        _string_tuple(self.tags, "eval case tags")
        if not isinstance(self.notes, str):
            raise EvalValidationError("eval case notes must be a string")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping with a stable key order."""
        return {
            "id": self.id,
            "prompt": self.prompt,
            "budget": self.budget.to_dict(),
            "expect": self.expect.to_dict(),
            "tags": list(self.tags),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, raw: object) -> EvalCase:
        """Parse one case mapping, refusing unknown fields."""
        data = _mapping(raw, "eval case")
        _reject_unknown(data, {"id", "prompt", "budget", "expect", "tags", "notes"}, "eval case")
        case_id = _text(_require_field(data, "id", "eval case"), "eval case id")
        prompt = _text(_require_field(data, "prompt", "eval case"), "eval case prompt")
        return cls(
            id=case_id,
            prompt=prompt,
            budget=EvalBudget.from_dict(data.get("budget", {})),
            expect=EvalExpectation.from_dict(data.get("expect", {})),
            tags=_string_tuple(data.get("tags"), "eval case tags"),
            notes=""
            if data.get("notes") is None
            else _text(data["notes"], "eval case notes", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One tool call observed during a run, as seen from outside the agent."""

    name: str
    arguments: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.name, "observed tool call name")
        object.__setattr__(
            self, "arguments", _json_mapping(self.arguments or {}, "observed tool call arguments")
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping."""
        return {"name": self.name, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, raw: object) -> ToolInvocation:
        """Parse one observed tool call, refusing unknown fields."""
        data = _mapping(raw, "observed tool call")
        _reject_unknown(data, {"name", "arguments"}, "observed tool call")
        name = _text(_require_field(data, "name", "observed tool call"), "observed tool call name")
        return cls(
            name=name, arguments=_json_mapping(data.get("arguments", {}), "observed tool call arguments")
        )


@dataclass(frozen=True, slots=True)
class EvalOutcome:
    """Everything the evaluator observes from one runner execution.

    A real AgentRunner adapter fills these fields from the agent session, tool
    calls and long-term memory records; a scripted runner replays a recorded
    outcome.  error carries a terminal runner failure, not a recovered tool error.
    """

    final_text: str = ""
    tool_calls: tuple[ToolInvocation, ...] = ()
    steps: int = 0
    tokens: int | None = None
    refused: bool = False
    budget_exceeded: bool = False
    recovered: bool = False
    memory: tuple[MemoryRecord, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.final_text, str):
            raise EvalValidationError("eval outcome final_text must be a string")
        if not isinstance(self.tool_calls, tuple) or not all(
            isinstance(item, ToolInvocation) for item in self.tool_calls
        ):
            raise EvalValidationError("eval outcome tool_calls must be a tuple of ToolInvocation")
        _count(self.steps, "eval outcome steps")
        _optional_count(self.tokens, "eval outcome tokens")
        for name in ("refused", "budget_exceeded", "recovered"):
            _bool(getattr(self, name), f"eval outcome {name}")
        if not isinstance(self.memory, tuple) or not all(
            isinstance(item, MemoryRecord) for item in self.memory
        ):
            raise EvalValidationError("eval outcome memory must be a tuple of MemoryRecord")
        _optional_text(self.error, "eval outcome error")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping with a stable key order."""
        return {
            "final_text": self.final_text,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "steps": self.steps,
            "tokens": self.tokens,
            "refused": self.refused,
            "budget_exceeded": self.budget_exceeded,
            "recovered": self.recovered,
            "memory": [record.to_dict() for record in self.memory],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: object) -> EvalOutcome:
        """Parse one outcome mapping, refusing unknown fields."""
        data = _mapping(raw, "eval outcome")
        allowed = {
            "final_text",
            "tool_calls",
            "steps",
            "tokens",
            "refused",
            "budget_exceeded",
            "recovered",
            "memory",
            "error",
        }
        _reject_unknown(data, allowed, "eval outcome")
        raw_calls = data.get("tool_calls", [])
        if not isinstance(raw_calls, (list, tuple)):
            raise EvalValidationError("eval outcome tool_calls must be a list")
        raw_steps = data.get("steps", 0)
        final_text = data.get("final_text", "")
        if not isinstance(final_text, str):
            raise EvalValidationError("eval outcome final_text must be a string")
        return cls(
            final_text=final_text,
            tool_calls=tuple(ToolInvocation.from_dict(item) for item in raw_calls),
            steps=_count(raw_steps, "eval outcome steps"),
            tokens=_optional_count(data.get("tokens"), "eval outcome tokens"),
            refused=_bool(data.get("refused", False), "eval outcome refused"),
            budget_exceeded=_bool(data.get("budget_exceeded", False), "eval outcome budget_exceeded"),
            recovered=_bool(data.get("recovered", False), "eval outcome recovered"),
            memory=_memory_records(data.get("memory"), "eval outcome memory"),
            error=_optional_text(data.get("error"), "eval outcome error"),
        )


@dataclass(frozen=True, slots=True)
class EvalCheck:
    """One named pass/fail observation produced while scoring a case."""

    name: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.name, "eval check name")
        _bool(self.passed, "eval check passed")
        if not isinstance(self.detail, str):
            raise EvalValidationError("eval check detail must be a string")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping."""
        return {"name": self.name, "passed": self.passed, "detail": self.detail}

    @classmethod
    def from_dict(cls, raw: object) -> EvalCheck:
        """Parse one check mapping, refusing unknown fields."""
        data = _mapping(raw, "eval check")
        _reject_unknown(data, {"name", "passed", "detail"}, "eval check")
        detail_value = data.get("detail", "")
        return cls(
            name=_text(_require_field(data, "name", "eval check"), "eval check name"),
            passed=_bool(_require_field(data, "passed", "eval check"), "eval check passed"),
            detail=detail_value if isinstance(detail_value, str) else "",
        )


@dataclass(frozen=True, slots=True)
class EvalResult:
    """Deterministic verdict for one case: checks plus the observed outcome."""

    case_id: str
    passed: bool
    checks: tuple[EvalCheck, ...]
    outcome: EvalOutcome | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        case_id = _text(self.case_id, "eval result case id")
        if not CASE_ID_RE.fullmatch(case_id):
            raise EvalValidationError("eval result case id must be a lowercase slug")
        _bool(self.passed, "eval result passed")
        if not isinstance(self.checks, tuple) or not all(isinstance(item, EvalCheck) for item in self.checks):
            raise EvalValidationError("eval result checks must be a tuple of EvalCheck")
        _optional_text(self.error, "eval result error")
        if self.outcome is not None and not isinstance(self.outcome, EvalOutcome):
            raise EvalValidationError("eval result outcome must be an EvalOutcome or null")
        expected = self.error is None and all(check.passed for check in self.checks)
        if expected != self.passed:
            raise EvalValidationError("eval result passed does not match its checks and error")

    @property
    def failed_checks(self) -> tuple[EvalCheck, ...]:
        """Return the checks that did not pass, in their original order."""
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping with a stable key order."""
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
            "outcome": None if self.outcome is None else self.outcome.to_dict(),
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: object) -> EvalResult:
        """Parse one result mapping, refusing unknown fields."""
        data = _mapping(raw, "eval result")
        _reject_unknown(data, {"case_id", "passed", "checks", "outcome", "error"}, "eval result")
        raw_checks = data.get("checks", [])
        if not isinstance(raw_checks, (list, tuple)):
            raise EvalValidationError("eval result checks must be a list")
        outcome = data.get("outcome")
        return cls(
            case_id=_text(_require_field(data, "case_id", "eval result"), "eval result case id"),
            passed=_bool(_require_field(data, "passed", "eval result"), "eval result passed"),
            checks=tuple(EvalCheck.from_dict(item) for item in raw_checks),
            outcome=None if outcome is None else EvalOutcome.from_dict(outcome),
            error=_optional_text(data.get("error"), "eval result error"),
        )


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Sorted, duplicate-free collection of case results for one suite run."""

    results: tuple[EvalResult, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.results, tuple) or not all(
            isinstance(item, EvalResult) for item in self.results
        ):
            raise EvalValidationError("eval report results must be a tuple of EvalResult")
        ids = [result.case_id for result in self.results]
        if len(set(ids)) != len(ids):
            raise EvalValidationError("eval report contains duplicate case ids")
        if ids != sorted(ids):
            raise EvalValidationError("eval report results must be sorted by case id")

    @property
    def total(self) -> int:
        """Number of cases in the report."""
        return len(self.results)

    @property
    def passed_count(self) -> int:
        """Number of passing cases."""
        return sum(1 for result in self.results if result.passed)

    @property
    def failed_count(self) -> int:
        """Number of failing cases."""
        return self.total - self.passed_count

    @property
    def all_passed(self) -> bool:
        """True when every case passed (an empty suite also passes)."""
        return self.failed_count == 0

    @property
    def failed_case_ids(self) -> tuple[str, ...]:
        """Ids of failing cases, in sorted order."""
        return tuple(result.case_id for result in self.results if not result.passed)

    def failure_summary(self) -> tuple[str, ...]:
        """Return one deterministic human-readable line per failing case."""
        lines: list[str] = []
        for result in self.results:
            if result.passed:
                continue
            details = "; ".join(
                f"{check.name}: {check.detail}" if check.detail else check.name
                for check in result.failed_checks
            )
            lines.append(f"{result.case_id}: {details}" if details else f"{result.case_id}: failed")
        return tuple(lines)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping with a stable key order."""
        return {
            "results": [result.to_dict() for result in self.results],
            "total": self.total,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "all_passed": self.all_passed,
            "failed_case_ids": list(self.failed_case_ids),
        }

    @classmethod
    def from_dict(cls, raw: object) -> EvalReport:
        """Parse one report mapping, refusing unknown fields."""
        data = _mapping(raw, "eval report")
        _reject_unknown(
            data,
            {"results", "total", "passed", "failed", "all_passed", "failed_case_ids"},
            "eval report",
        )
        raw_results = data.get("results", [])
        if not isinstance(raw_results, (list, tuple)):
            raise EvalValidationError("eval report results must be a list")
        return cls(results=tuple(EvalResult.from_dict(item) for item in raw_results))


__all__ = [
    "CASE_ID_RE",
    "DEFAULT_MAX_STEPS",
    "MAX_EVAL_STEPS",
    "MEMORY_PROVENANCE_FIELDS",
    "EvalBudget",
    "EvalCase",
    "EvalCheck",
    "EvalExpectation",
    "EvalOutcome",
    "EvalReport",
    "EvalResult",
    "ExpectedToolCall",
    "ToolInvocation",
]
