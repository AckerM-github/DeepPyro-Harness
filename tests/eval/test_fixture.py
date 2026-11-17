"""Tests for golden fixture loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.eval import (
    GoldenEntry,
    load_eval_cases,
    load_golden_entries,
    load_json_lines,
    load_recorded_outcomes,
)
from deeppyro.eval.errors import EvalFixtureError, EvalValidationError
from deeppyro.eval.types import EvalCase, EvalOutcome

GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "eval" / "golden.jsonl"


def _write_fixture(tmp_path: Path, lines: list[str]) -> Path:
    target = tmp_path / "golden.jsonl"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def _entry_line(case_id: str = "case-001") -> str:
    case = EvalCase(id=case_id, prompt="do the thing")
    recorded = EvalOutcome(final_text="done")
    return json.dumps({"case": case.to_dict(), "recorded": recorded.to_dict()})


def test_load_json_lines_reads_every_line() -> None:
    records = load_json_lines(GOLDEN)
    assert len(records) == 20
    assert all(set(record) == {"case", "recorded"} for record in records)


def test_load_golden_entries_round_trip() -> None:
    entries = load_golden_entries(GOLDEN)
    assert len(entries) == 20
    assert len({entry.case.id for entry in entries}) == 20
    for entry in entries:
        assert isinstance(entry, GoldenEntry)
        assert GoldenEntry.from_dict(entry.to_dict()) == entry


def test_load_eval_cases_is_sorted() -> None:
    cases = load_eval_cases(GOLDEN)
    ids = [case.id for case in cases]
    assert ids == sorted(ids)
    assert len(ids) == 20


def test_load_recorded_outcomes_matches_cases() -> None:
    outcomes = load_recorded_outcomes(GOLDEN)
    cases = load_eval_cases(GOLDEN)
    assert set(outcomes) == {case.id for case in cases}
    assert all(isinstance(outcome, EvalOutcome) for outcome in outcomes.values())


def test_blank_line_is_a_fixture_error(tmp_path: Path) -> None:
    target = _write_fixture(tmp_path, [_entry_line(), ""])
    with pytest.raises(EvalFixtureError, match="blank line"):
        load_json_lines(target)


def test_invalid_json_is_a_fixture_error(tmp_path: Path) -> None:
    target = _write_fixture(tmp_path, ["{not json"])
    with pytest.raises(EvalFixtureError, match="invalid JSON"):
        load_json_lines(target)


def test_non_object_line_is_a_fixture_error(tmp_path: Path) -> None:
    target = _write_fixture(tmp_path, ["[1, 2]"])
    with pytest.raises(EvalFixtureError, match="expected a JSON object"):
        load_json_lines(target)


def test_empty_file_is_a_fixture_error(tmp_path: Path) -> None:
    target = tmp_path / "empty.jsonl"
    target.write_text("", encoding="utf-8")
    with pytest.raises(EvalFixtureError, match="contains no entries"):
        load_json_lines(target)


def test_missing_file_is_a_fixture_error(tmp_path: Path) -> None:
    with pytest.raises(EvalFixtureError, match="cannot read eval fixture"):
        load_json_lines(tmp_path / "missing.jsonl")


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    target = _write_fixture(tmp_path, [_entry_line("same-case"), _entry_line("same-case")])
    with pytest.raises(EvalFixtureError, match="duplicate case id"):
        load_golden_entries(target)


def test_unknown_golden_field_is_rejected(tmp_path: Path) -> None:
    entry = {"case": EvalCase(id="case-001", prompt="x").to_dict(), "recorded": {}, "extra": 1}
    target = _write_fixture(tmp_path, [json.dumps(entry)])
    with pytest.raises(EvalValidationError, match="unknown field"):
        load_golden_entries(target)


def test_golden_entry_from_dict_requires_both_fields() -> None:
    with pytest.raises(EvalValidationError, match="lacks required field"):
        GoldenEntry.from_dict({"case": EvalCase(id="case-001", prompt="x").to_dict()})
    with pytest.raises(EvalValidationError, match="must be a JSON object"):
        GoldenEntry.from_dict([])
