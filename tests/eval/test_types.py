"""Tests for the eval data model, validation and report invariants."""

from __future__ import annotations

import pytest

from deeppyro.eval import (
    DEFAULT_MAX_STEPS,
    MAX_EVAL_STEPS,
    EvalBudget,
    EvalCase,
    EvalCheck,
    EvalExpectation,
    EvalOutcome,
    EvalReport,
    EvalResult,
    ExpectedToolCall,
    ToolInvocation,
    load_golden_entries,
)
from deeppyro.eval.errors import EvalValidationError
from deeppyro.memory import MemoryRecord

MEMORY = MemoryRecord.from_dict(
    {
        "id": "mem_0123456789abcdef",
        "content": "Long-term memory uses SQLite FTS5.",
        "provenance": {
            "content_sha256": "a" * 64,
            "created_at": "2026-11-17T12:00:00.000Z",
            "source_session_id": "session-aaaa",
            "source_message_id": "msg_0123456789abcdef",
            "run_id": "run_0123456789abcdef",
            "importance": "normal",
            "tags": ["memory"],
        },
    }
)


def test_eval_case_round_trip() -> None:
    case = EvalCase(
        id="case-001",
        prompt="Compute 4 + 5 with the add tool.",
        budget=EvalBudget(max_steps=4, max_tokens=256, max_seconds=2.5),
        expect=EvalExpectation(
            tool_calls=(ExpectedToolCall(name="add", arguments={"a": 4, "b": 5}),),
            final_text_contains=("9",),
            memory_provenance=("source_session_id", "content_sha256"),
            refused=False,
            budget_exceeded=False,
            recovered=False,
        ),
        tags=("tool", "text"),
        notes="round trip",
    )
    data = case.to_dict()
    assert data["id"] == "case-001"
    assert data["budget"] == {"max_steps": 4, "max_tokens": 256, "max_seconds": 2.5}
    assert EvalCase.from_dict(data) == case


def test_eval_budget_defaults_and_validation() -> None:
    budget = EvalBudget()
    assert budget.max_steps == DEFAULT_MAX_STEPS
    assert "max_tokens" not in budget.to_dict()
    assert "max_seconds" not in budget.to_dict()
    assert EvalBudget.from_dict({}) == budget
    with pytest.raises(EvalValidationError):
        EvalBudget(max_steps=0)
    with pytest.raises(EvalValidationError):
        EvalBudget(max_steps=MAX_EVAL_STEPS + 1)
    with pytest.raises(EvalValidationError):
        EvalBudget(max_tokens=0)
    with pytest.raises(EvalValidationError):
        EvalBudget(max_seconds=0)
    with pytest.raises(EvalValidationError):
        EvalBudget.from_dict({"max_steps": 4, "unknown": True})


def test_eval_case_validation() -> None:
    with pytest.raises(EvalValidationError):
        EvalCase(id="Case With Spaces", prompt="x")
    with pytest.raises(EvalValidationError):
        EvalCase(id="case-ok", prompt="   ")
    with pytest.raises(EvalValidationError):
        EvalCase(id="case-ok", prompt="x", tags=("",))
    with pytest.raises(EvalValidationError):
        ExpectedToolCall(name="")
    with pytest.raises(EvalValidationError):
        ToolInvocation(name="")
    with pytest.raises(EvalValidationError):
        EvalExpectation(memory_provenance=("not_a_provenance_field",))
    with pytest.raises(EvalValidationError):
        EvalCase.from_dict({**EvalCase(id="case-ok", prompt="x").to_dict(), "extra": 1})


def test_expectation_to_dict_keeps_null_flags() -> None:
    data = EvalExpectation().to_dict()
    assert data["refused"] is None
    assert data["budget_exceeded"] is None
    assert data["recovered"] is None
    assert data["allows_error"] is False
    assert data["tool_calls"] == []


def test_eval_outcome_round_trip() -> None:
    outcome = EvalOutcome(
        final_text="long-term memory uses FTS5",
        tool_calls=(ToolInvocation(name="search", arguments={"query": "memory"}),),
        steps=2,
        tokens=42,
        recovered=True,
        memory=(MEMORY,),
    )
    data = outcome.to_dict()
    assert data["tokens"] == 42
    assert data["memory"][0]["id"] == MEMORY.id
    assert EvalOutcome.from_dict(data) == outcome
    empty = EvalOutcome()
    assert EvalOutcome.from_dict(empty.to_dict()) == empty


def test_eval_result_consistency() -> None:
    passed = EvalResult(case_id="case-001", passed=True, checks=(EvalCheck("steps", True),))
    assert passed.failed_checks == ()
    failed = EvalResult(
        case_id="case-001",
        passed=False,
        checks=(EvalCheck("steps", False, "too many steps"), EvalCheck("tokens", True)),
    )
    assert [check.name for check in failed.failed_checks] == ["steps"]
    assert failed.to_dict()["outcome"] is None
    with pytest.raises(EvalValidationError):
        EvalResult(case_id="case-001", passed=True, checks=(EvalCheck("steps", False),))
    with pytest.raises(EvalValidationError):
        EvalResult(case_id="case-001", passed=True, checks=(), error="boom")
    errored = EvalResult(
        case_id="case-001",
        passed=False,
        checks=(EvalCheck("runner", False, "boom"),),
        error="boom",
    )
    assert errored.error == "boom"


def test_eval_report_sorted_and_aggregated() -> None:
    first = EvalResult(case_id="alpha-case", passed=True, checks=(EvalCheck("steps", True),))
    second = EvalResult(
        case_id="beta-case",
        passed=False,
        checks=(EvalCheck("final_text", False, "missing answer"),),
    )
    report = EvalReport(results=(first, second))
    assert report.total == 2
    assert report.passed_count == 1
    assert report.failed_count == 1
    assert report.all_passed is False
    assert report.failed_case_ids == ("beta-case",)
    assert report.failure_summary() == ("beta-case: final_text: missing answer",)
    assert EvalReport.from_dict(report.to_dict()) == report
    with pytest.raises(EvalValidationError):
        EvalReport(results=(second, first))
    with pytest.raises(EvalValidationError):
        EvalReport(results=(first, first))


def test_golden_fixture_counts_and_coverage() -> None:
    path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "tests"
        / "fixtures"
        / "eval"
        / "golden.jsonl"
    )
    entries = load_golden_entries(path)
    assert len(entries) == 20
    assert len({entry.case.id for entry in entries}) == 20
    tags = {tag for entry in entries for tag in entry.case.tags}
    assert {"tool", "text", "refusal", "budget", "recovery", "memory"} <= tags
    required = {call.name for entry in entries for call in entry.case.expect.tool_calls}
    assert required >= {"add", "read_file"}
    provenance = {field for entry in entries for field in entry.case.expect.memory_provenance}
    assert provenance == {
        "source_session_id",
        "source_message_id",
        "run_id",
        "created_at",
        "content_sha256",
        "importance",
        "tags",
    }
    for entry in entries:
        assert entry.case.expect.budget_exceeded is True or entry.recorded.budget_exceeded is False
        assert entry.recorded.memory == () or all(
            record.provenance.content_sha256 for record in entry.recorded.memory
        )
