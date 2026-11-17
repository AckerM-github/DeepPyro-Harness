"""Tests for eval scoring, fake runners and recorded golden replay."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deeppyro.eval import (
    AgentRunner,
    CallableAgentRunner,
    EvalBudget,
    EvalCase,
    EvalExpectation,
    EvalOutcome,
    EvalRunner,
    ExpectedToolCall,
    ScriptedEvalRunner,
    ToolInvocation,
    check_outcome,
    load_eval_cases,
    load_recorded_outcomes,
    run_recorded_golden,
)
from deeppyro.eval.errors import EvalValidationError
from deeppyro.memory import MemoryRecord

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval" / "golden.jsonl"


def _case(
    case_id: str = "case-001",
    *,
    budget: EvalBudget | None = None,
    expect: EvalExpectation | None = None,
) -> EvalCase:
    return EvalCase(
        id=case_id,
        prompt="do the thing",
        budget=budget if budget is not None else EvalBudget(),
        expect=expect if expect is not None else EvalExpectation(),
    )


def _memory(record_id: str = "mem_0123456789abcdef", content: str = "sqlite fts5 memory") -> MemoryRecord:
    return MemoryRecord.from_dict(
        {
            "id": record_id,
            "content": content,
            "provenance": {
                "content_sha256": "b" * 64,
                "created_at": "2026-11-17T12:00:00.000Z",
                "source_session_id": "session-aaaa",
                "importance": "normal",
                "tags": ["memory"],
            },
        }
    )


def _memory_without_session() -> MemoryRecord:
    return MemoryRecord.from_dict(
        {
            "id": "mem_1111111111111111",
            "content": "bare memory",
            "provenance": {
                "content_sha256": "c" * 64,
                "created_at": "2026-11-17T12:00:00.000Z",
                "importance": "normal",
            },
        }
    )


def _check_names(case: EvalCase, outcome: EvalOutcome) -> list[str]:
    return [check.name for check in check_outcome(case, outcome)]


def test_check_outcome_passes_full_expectation() -> None:
    case = _case(
        budget=EvalBudget(max_steps=4, max_tokens=100),
        expect=EvalExpectation(
            tool_calls=(
                ExpectedToolCall(name="add", arguments={"a": 1, "b": 2}),
                ExpectedToolCall(name="read_file", arguments={"path": "notes.txt"}),
            ),
            final_text_contains=("42",),
            memory_contains=("sqlite",),
            memory_provenance=("content_sha256", "created_at"),
            refused=False,
            budget_exceeded=False,
            recovered=True,
        ),
    )
    outcome = EvalOutcome(
        final_text="the answer is 42",
        tool_calls=(
            ToolInvocation(name="add", arguments={"a": 1, "b": 2, "rounding": 0}),
            ToolInvocation(name="read_file", arguments={"path": "notes.txt"}),
        ),
        steps=3,
        tokens=50,
        recovered=True,
        memory=(_memory(),),
    )
    checks = check_outcome(case, outcome)
    assert all(check.passed for check in checks), [check for check in checks if not check.passed]
    assert [check.name for check in checks] == [
        "steps",
        "tokens",
        "error",
        "tool_calls",
        "final_text",
        "refused",
        "budget_exceeded",
        "recovered",
        "memory_contains",
        "memory_provenance",
    ]


def test_tool_calls_must_appear_in_order() -> None:
    case = _case(
        expect=EvalExpectation(
            tool_calls=(
                ExpectedToolCall(name="add"),
                ExpectedToolCall(name="read_file"),
            )
        )
    )
    outcome = EvalOutcome(
        tool_calls=(ToolInvocation(name="read_file"), ToolInvocation(name="add")),
    )
    checks = {check.name: check for check in check_outcome(case, outcome)}
    assert checks["tool_calls"].passed is False
    assert "missing expected tool calls in order" in checks["tool_calls"].detail


def test_arguments_are_subset_matched() -> None:
    case = _case(expect=EvalExpectation(tool_calls=(ExpectedToolCall(name="add", arguments={"a": 1}),)))
    passing = EvalOutcome(tool_calls=(ToolInvocation(name="add", arguments={"a": 1, "b": 2}),))
    failing = EvalOutcome(tool_calls=(ToolInvocation(name="add", arguments={"a": 9}),))
    assert {check.name: check for check in check_outcome(case, passing)}["tool_calls"].passed is True
    assert {check.name: check for check in check_outcome(case, failing)}["tool_calls"].passed is False


def test_steps_and_tokens_budgets() -> None:
    case = _case(budget=EvalBudget(max_steps=2, max_tokens=10))
    over_steps = EvalOutcome(steps=3, tokens=None)
    checks = {check.name: check for check in check_outcome(case, over_steps)}
    assert checks["steps"].passed is False
    assert checks["steps"].detail == "steps 3 exceed budget 2"
    assert "tokens" not in checks
    over_tokens = EvalOutcome(steps=2, tokens=11)
    tokens = {check.name: check for check in check_outcome(case, over_tokens)}["tokens"]
    assert tokens.passed is False
    assert tokens.detail == "tokens 11 exceed budget 10"


def test_error_expectations() -> None:
    expecting_denied = _case(expect=EvalExpectation(error_contains=("denied",)))
    allowed = _case(expect=EvalExpectation(allows_error=True))
    assert {c.name: c for c in check_outcome(expecting_denied, EvalOutcome(error="permission denied"))}[
        "error"
    ].passed
    missing = {c.name: c for c in check_outcome(expecting_denied, EvalOutcome())}["error"]
    assert missing.passed is False
    assert "expected an error" in missing.detail
    unexpected = {c.name: c for c in check_outcome(_case(), EvalOutcome(error="boom"))}["error"]
    assert unexpected.passed is False
    assert "unexpected error" in unexpected.detail
    assert {c.name: c for c in check_outcome(allowed, EvalOutcome(error="boom"))}["error"].passed


def test_memory_checks_report_missing_provenance() -> None:
    case = _case(expect=EvalExpectation(memory_provenance=("source_session_id",)))
    missing_field = {
        c.name: c for c in check_outcome(case, EvalOutcome(memory=(_memory_without_session(),)))
    }["memory_provenance"]
    assert missing_field.passed is False
    assert "lacks provenance field source_session_id" in missing_field.detail
    no_records = {c.name: c for c in check_outcome(case, EvalOutcome())}["memory_provenance"]
    assert no_records.passed is False
    assert "wrote none" in no_records.detail


def test_memory_contains_covers_all_records() -> None:
    case = _case(expect=EvalExpectation(memory_contains=("sqlite", "fts5")))
    outcome = EvalOutcome(memory=(_memory(),))
    assert {c.name: c for c in check_outcome(case, outcome)}["memory_contains"].passed is True
    missing = EvalOutcome(memory=(_memory(content="something else"),))
    check = {c.name: c for c in check_outcome(case, missing)}["memory_contains"]
    assert check.passed is False
    assert "recorded memory misses" in check.detail


async def test_scripted_runner_scores_recorded_outcome() -> None:
    case = _case(expect=EvalExpectation(final_text_contains=("done",)))
    outcome = EvalOutcome(final_text="all done")
    runner = EvalRunner(ScriptedEvalRunner({case.id: outcome}))
    result = await runner.run_case(case)
    assert result.passed is True
    assert result.outcome == outcome


async def test_unknown_recorded_outcome_is_a_failed_run() -> None:
    case = _case()
    result = await EvalRunner(ScriptedEvalRunner({})).run_case(case)
    assert result.passed is False
    assert result.outcome is not None
    assert result.outcome.error is not None
    assert "no recorded outcome" in result.outcome.error
    failed = [check for check in result.checks if not check.passed]
    assert [check.name for check in failed] == ["error"]


async def test_runner_exception_becomes_failed_result() -> None:
    async def explode(case: EvalCase) -> EvalOutcome:
        raise RuntimeError("boom")

    result = await EvalRunner(CallableAgentRunner(explode)).run_case(_case())
    assert result.passed is False
    assert result.error == "RuntimeError: boom"
    assert result.outcome is None
    assert result.checks[0].name == "runner"


async def test_runner_timeout_becomes_failed_result() -> None:
    async def slow(case: EvalCase) -> EvalOutcome:
        await asyncio.sleep(0.05)
        return EvalOutcome(final_text="late")

    result = await EvalRunner(CallableAgentRunner(slow), timeout_seconds=0.001).run_case(_case())
    assert result.passed is False
    assert result.error is not None
    assert result.error.startswith("timed out after")


async def test_per_case_time_budget_is_enforced() -> None:
    async def slow(case: EvalCase) -> EvalOutcome:
        await asyncio.sleep(0.05)
        return EvalOutcome()

    case = _case(budget=EvalBudget(max_steps=4, max_seconds=0.001))
    result = await EvalRunner(CallableAgentRunner(slow)).run_case(case)
    assert result.passed is False
    assert result.error is not None
    assert "timed out after" in result.error


async def test_run_cases_sorts_and_is_deterministic() -> None:
    zeta = _case("zeta-case", expect=EvalExpectation(final_text_contains=("zeta",)))
    alpha = _case("alpha-case", expect=EvalExpectation(final_text_contains=("alpha",)))
    runner = EvalRunner(
        ScriptedEvalRunner(
            {
                zeta.id: EvalOutcome(final_text="zeta result"),
                alpha.id: EvalOutcome(final_text="alpha result"),
            }
        )
    )
    first = await runner.run_cases((zeta, alpha))
    second = await runner.run_cases((alpha, zeta))
    assert [result.case_id for result in first.results] == ["alpha-case", "zeta-case"]
    assert first.all_passed is True
    assert first.to_dict() == second.to_dict()


async def test_run_cases_rejects_duplicates_and_non_cases() -> None:
    runner = EvalRunner(ScriptedEvalRunner({}))
    case = _case()
    with pytest.raises(EvalValidationError, match="duplicate eval case id"):
        await runner.run_cases((case, case))
    with pytest.raises(EvalValidationError, match="must contain EvalCase"):
        await runner.run_cases((case, "not-a-case"))  # type: ignore[arg-type]


def test_fake_runners_satisfy_protocol() -> None:
    assert isinstance(ScriptedEvalRunner({}), AgentRunner)

    async def run(case: EvalCase) -> EvalOutcome:
        return EvalOutcome()

    assert isinstance(CallableAgentRunner(run), AgentRunner)
    with pytest.raises(EvalValidationError, match="must implement the AgentRunner protocol"):
        EvalRunner(object())  # type: ignore[arg-type]
    with pytest.raises(EvalValidationError, match="must be a positive number"):
        EvalRunner(ScriptedEvalRunner({}), timeout_seconds=0)
    with pytest.raises(EvalValidationError, match="must be callable"):
        CallableAgentRunner(42)  # type: ignore[arg-type]
    with pytest.raises(EvalValidationError, match="must be an EvalOutcome"):
        ScriptedEvalRunner({"case-001": "nope"})  # type: ignore[dict-item]


async def test_recorded_golden_fixture_all_passes() -> None:
    report = await run_recorded_golden(GOLDEN)
    assert report.total == 20
    assert report.passed_count == 20
    assert report.failed_case_ids == ()
    assert report.all_passed is True
    assert [result.case_id for result in report.results] == sorted(
        result.case_id for result in report.results
    )
    assert all(check.passed for result in report.results for check in result.checks)


def test_recorded_outcomes_score_clean_individually() -> None:
    cases = load_eval_cases(GOLDEN)
    outcomes = load_recorded_outcomes(GOLDEN)
    for case in cases:
        checks = check_outcome(case, outcomes[case.id])
        assert all(check.passed for check in checks), (case.id, [c for c in checks if not c.passed])
