"""Deterministic, provider-free eval execution and scoring.

EvalRunner drives an AgentRunner (a fake provider, a recorded fixture replay or a
real agent adapter) one case at a time and scores the returned EvalOutcome
against the case expectations.  Scoring is a pure function, so a recorded
outcome always yields the same EvalResult.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from deeppyro.eval.errors import EvalValidationError
from deeppyro.eval.fixture import load_golden_entries
from deeppyro.eval.types import (
    MEMORY_PROVENANCE_FIELDS,
    EvalCase,
    EvalCheck,
    EvalExpectation,
    EvalOutcome,
    EvalReport,
    EvalResult,
)
from deeppyro.memory.backend import MemoryRecord


@runtime_checkable
class AgentRunner(Protocol):
    """The one-method seam every eval runner implements.

    Real adapters wrap an AgentSession and translate its messages, tool calls and
    long-term memory writes into an EvalOutcome; tests use ScriptedEvalRunner or
    CallableAgentRunner.  The runner must not inspect case.expect: expectations
    are only read by the scorer.
    """

    async def run(self, case: EvalCase) -> EvalOutcome:
        """Execute one case and report what happened."""
        ...


def _describe(value: object, *, limit: int = 160) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _arguments_match(expected: dict[str, object], observed: dict[str, object]) -> bool:
    return all(key in observed and observed[key] == value for key, value in expected.items())


def _tool_calls_check(expectation: EvalExpectation, outcome: EvalOutcome) -> EvalCheck:
    cursor = 0
    missing: list[str] = []
    for expected in expectation.tool_calls:
        while cursor < len(outcome.tool_calls):
            observed = outcome.tool_calls[cursor]
            cursor += 1
            if observed.name == expected.name and _arguments_match(expected.arguments, observed.arguments):
                break
        else:
            missing.append(_describe(expected.to_dict()))
    if missing:
        return EvalCheck("tool_calls", False, "missing expected tool calls in order: " + ", ".join(missing))
    return EvalCheck("tool_calls", True)


def _final_text_check(expectation: EvalExpectation, outcome: EvalOutcome) -> EvalCheck:
    missing = [needle for needle in expectation.final_text_contains if needle not in outcome.final_text]
    if missing:
        return EvalCheck(
            "final_text",
            False,
            "final text misses: " + ", ".join(_describe(needle) for needle in missing),
        )
    return EvalCheck("final_text", True)


def _error_check(expectation: EvalExpectation, outcome: EvalOutcome) -> EvalCheck:
    if expectation.error_contains:
        if outcome.error is None:
            return EvalCheck("error", False, "expected an error but the run succeeded")
        missing = [needle for needle in expectation.error_contains if needle not in outcome.error]
        if missing:
            return EvalCheck(
                "error",
                False,
                "error misses: " + ", ".join(_describe(needle) for needle in missing),
            )
        return EvalCheck("error", True)
    if not expectation.allows_error and outcome.error is not None:
        return EvalCheck("error", False, "unexpected error: " + _describe(outcome.error))
    return EvalCheck("error", True)


def _memory_contains_check(expectation: EvalExpectation, outcome: EvalOutcome) -> EvalCheck:
    haystack = "\n".join(record.content for record in outcome.memory)
    missing = [needle for needle in expectation.memory_contains if needle not in haystack]
    if missing:
        return EvalCheck(
            "memory_contains",
            False,
            "recorded memory misses: " + ", ".join(_describe(needle) for needle in missing),
        )
    return EvalCheck("memory_contains", True)


def _provenance_present(record: MemoryRecord, field_name: str) -> bool:
    value = record.provenance_value(field_name)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (tuple, list)):
        return len(value) > 0
    return True


def _memory_provenance_check(expectation: EvalExpectation, outcome: EvalOutcome) -> EvalCheck:
    if not outcome.memory:
        return EvalCheck("memory_provenance", False, "expected memory records but the run wrote none")
    required = [field for field in MEMORY_PROVENANCE_FIELDS if field in expectation.memory_provenance]
    for index, record in enumerate(outcome.memory):
        for field_name in required:
            if not _provenance_present(record, field_name):
                return EvalCheck(
                    "memory_provenance",
                    False,
                    f"record {index} ({record.id}) lacks provenance field {field_name}",
                )
    return EvalCheck("memory_provenance", True)


def check_outcome(case: EvalCase, outcome: EvalOutcome) -> tuple[EvalCheck, ...]:
    """Score one outcome against one case, returning checks in a fixed order.

    The check order is part of the deterministic contract and must not depend on
    which expectations are set beyond the documented conditional checks.
    """
    if not isinstance(case, EvalCase):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise EvalValidationError("check_outcome case must be an EvalCase")
    if not isinstance(outcome, EvalOutcome):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise EvalValidationError("check_outcome outcome must be an EvalOutcome")
    budget = case.budget
    expectation = case.expect
    checks: list[EvalCheck] = [
        EvalCheck(
            "steps",
            outcome.steps <= budget.max_steps,
            f"steps {outcome.steps} exceed budget {budget.max_steps}",
        )
    ]
    if budget.max_tokens is not None and outcome.tokens is not None:
        checks.append(
            EvalCheck(
                "tokens",
                outcome.tokens <= budget.max_tokens,
                f"tokens {outcome.tokens} exceed budget {budget.max_tokens}",
            )
        )
    checks.extend(
        [
            _error_check(expectation, outcome),
            _tool_calls_check(expectation, outcome),
            _final_text_check(expectation, outcome),
        ]
    )
    for name in ("refused", "budget_exceeded", "recovered"):
        expected_flag = getattr(expectation, name)
        if expected_flag is not None:
            observed_flag = getattr(outcome, name)
            checks.append(
                EvalCheck(
                    name,
                    observed_flag == expected_flag,
                    f"expected {name}={expected_flag} but observed {observed_flag}",
                )
            )
    if expectation.memory_contains:
        checks.append(_memory_contains_check(expectation, outcome))
    if expectation.memory_provenance:
        checks.append(_memory_provenance_check(expectation, outcome))
    return tuple(checks)


class EvalRunner:
    """Runs eval cases through one AgentRunner and scores the outcomes.

    Cases run sequentially so a fake provider does not need concurrency and so
    debug output is readable.  per-case max_seconds bounds the runner call; a
    timeout is a failed result, not an exception.
    """

    def __init__(self, runner: AgentRunner, *, timeout_seconds: float | None = None) -> None:
        if not isinstance(runner, AgentRunner):
            raise EvalValidationError("eval runner must implement the AgentRunner protocol")
        if timeout_seconds is not None:
            if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
                raise EvalValidationError("eval runner timeout_seconds must be a positive number")
            if timeout_seconds <= 0:
                raise EvalValidationError("eval runner timeout_seconds must be a positive number")
        self._runner = runner
        self._timeout_seconds = None if timeout_seconds is None else float(timeout_seconds)

    @property
    def runner(self) -> AgentRunner:
        """The underlying runner, exposed for test drivers."""
        return self._runner

    @property
    def timeout_seconds(self) -> float | None:
        """Configured global timeout override, if any."""
        return self._timeout_seconds

    def _timeout_for(self, case: EvalCase) -> float | None:
        return self._timeout_seconds if self._timeout_seconds is not None else case.budget.max_seconds

    async def run_case(self, case: EvalCase) -> EvalResult:
        """Run and score one case.  Runner crashes and timeouts become failures."""
        if not isinstance(case, EvalCase):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise EvalValidationError("eval runner case must be an EvalCase")
        timeout = self._timeout_for(case)
        try:
            if timeout is None:
                outcome = await self._runner.run(case)
            else:
                outcome = await asyncio.wait_for(self._runner.run(case), timeout)
        except TimeoutError:
            message = "runner timed out" if timeout is None else f"timed out after {timeout:g}s"
            return EvalResult(
                case_id=case.id,
                passed=False,
                checks=(EvalCheck("runner", False, message),),
                outcome=None,
                error=message,
            )
        except Exception as exc:  # noqa: BLE001 - runner crashes become failed results
            message = f"{type(exc).__name__}: {exc}"
            return EvalResult(
                case_id=case.id,
                passed=False,
                checks=(EvalCheck("runner", False, message),),
                outcome=None,
                error=message,
            )
        checks = check_outcome(case, outcome)
        return EvalResult(
            case_id=case.id,
            passed=all(check.passed for check in checks),
            checks=checks,
            outcome=outcome,
        )

    async def run_cases(self, cases: Sequence[EvalCase]) -> EvalReport:
        """Run every case in sorted case-id order and return a sorted report."""
        prepared = self._normalize_cases(cases)
        results: list[EvalResult] = []
        for case in prepared:
            results.append(await self.run_case(case))
        return EvalReport(results=tuple(results))

    def _normalize_cases(self, cases: Sequence[EvalCase]) -> tuple[EvalCase, ...]:
        if isinstance(cases, (str, bytes)):
            raise EvalValidationError("eval cases must be a sequence of EvalCase")
        prepared: list[EvalCase] = []
        for case in cases:
            if not isinstance(case, EvalCase):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise EvalValidationError("eval cases must contain EvalCase values")
            prepared.append(case)
        ids = [case.id for case in prepared]
        duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
        if duplicates:
            raise EvalValidationError(f"duplicate eval case id {duplicates[0]!r}")
        return tuple(sorted(prepared, key=lambda case: case.id))


class ScriptedEvalRunner:
    """Fake AgentRunner that replays recorded outcomes keyed by case id."""

    def __init__(self, outcomes: Mapping[str, EvalOutcome]) -> None:
        if not isinstance(outcomes, Mapping):
            raise EvalValidationError("scripted eval outcomes must be a mapping of case id to outcome")
        copied: dict[str, EvalOutcome] = {}
        for case_id, outcome in outcomes.items():
            if not isinstance(case_id, str) or not case_id:
                raise EvalValidationError("scripted eval outcome keys must be non-empty case ids")
            if not isinstance(outcome, EvalOutcome):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise EvalValidationError(f"scripted eval outcome for {case_id!r} must be an EvalOutcome")
            copied[case_id] = outcome
        self._outcomes = copied

    @property
    def outcomes(self) -> Mapping[str, EvalOutcome]:
        """Read-only view of the recorded outcomes."""
        return dict(self._outcomes)

    async def run(self, case: EvalCase) -> EvalOutcome:
        """Return the recorded outcome, or an error outcome for an unknown case."""
        outcome = self._outcomes.get(case.id)
        if outcome is None:
            return EvalOutcome(error=f"no recorded outcome for case {case.id!r}")
        return outcome


class CallableAgentRunner:
    """Adapt an async callable to the AgentRunner protocol (tests, ad hoc)."""

    def __init__(self, run: Callable[[EvalCase], Awaitable[EvalOutcome]]) -> None:
        if not callable(run):
            raise EvalValidationError("callable eval runner must be callable")
        self._run = run

    async def run(self, case: EvalCase) -> EvalOutcome:
        """Delegate to the wrapped callable."""
        return await self._run(case)


async def run_recorded_golden(path: str | Path, *, timeout_seconds: float | None = None) -> EvalReport:
    """Score a golden fixture entirely from its recorded outcomes (no model calls)."""
    entries = load_golden_entries(path)
    outcomes = {entry.case.id: entry.recorded for entry in entries}
    runner = EvalRunner(ScriptedEvalRunner(outcomes), timeout_seconds=timeout_seconds)
    return await runner.run_cases(tuple(entry.case for entry in entries))


__all__ = [
    "AgentRunner",
    "CallableAgentRunner",
    "EvalRunner",
    "ScriptedEvalRunner",
    "check_outcome",
    "run_recorded_golden",
]
