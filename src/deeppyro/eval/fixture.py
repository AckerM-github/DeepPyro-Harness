"""Golden fixture loading for the offline eval suite.

A golden fixture is UTF-8 JSON Lines.  Each line pairs an EvalCase with the
EvalOutcome that a reference run recorded for it.  Replaying the recorded
outcomes through EvalRunner exercises the real scorer without any model call,
which is what the non-blocking native CI workflow does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from deeppyro.eval.errors import EvalFixtureError, EvalValidationError
from deeppyro.eval.types import EvalCase, EvalOutcome


def _as_path(path: str | Path) -> Path:
    return path if isinstance(path, Path) else Path(path)


def load_json_lines(path: str | Path) -> tuple[dict[str, object], ...]:
    """Read a JSON Lines file and return every line as a mapping.

    Blank lines and non-object lines are fixture errors: a typo in a golden file
    must fail loudly instead of silently dropping a case.
    """
    target = _as_path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalFixtureError(f"cannot read eval fixture {target}: {exc}") from exc
    records: list[dict[str, object]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            raise EvalFixtureError(f"{target}:{number}: blank line in JSON Lines fixture")
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise EvalFixtureError(f"{target}:{number}: invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise EvalFixtureError(f"{target}:{number}: expected a JSON object")
        records.append(raw)
    if not records:
        raise EvalFixtureError(f"eval fixture {target} contains no entries")
    return tuple(records)


@dataclass(frozen=True, slots=True)
class GoldenEntry:
    """One eval case plus the outcome a reference run recorded for it."""

    case: EvalCase
    recorded: EvalOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.case, EvalCase):
            raise EvalValidationError("golden entry case must be an EvalCase")
        if not isinstance(self.recorded, EvalOutcome):
            raise EvalValidationError("golden entry recorded must be an EvalOutcome")

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-ready mapping with a stable key order."""
        return {"case": self.case.to_dict(), "recorded": self.recorded.to_dict()}

    @classmethod
    def from_dict(cls, raw: object) -> GoldenEntry:
        """Parse one golden entry, refusing unknown fields."""
        if not isinstance(raw, dict):
            raise EvalValidationError("golden entry must be a JSON object")
        unknown = sorted(set(raw) - {"case", "recorded"})
        if unknown:
            raise EvalValidationError(f"golden entry has unknown field {unknown[0]!r}")
        missing = sorted({"case", "recorded"} - set(raw))
        if missing:
            raise EvalValidationError(f"golden entry lacks required field {missing[0]!r}")
        return cls(
            case=EvalCase.from_dict(raw["case"]),
            recorded=EvalOutcome.from_dict(raw["recorded"]),
        )


def load_golden_entries(path: str | Path) -> tuple[GoldenEntry, ...]:
    """Load and validate a golden fixture file."""
    entries = tuple(GoldenEntry.from_dict(raw) for raw in load_json_lines(path))
    ids = [entry.case.id for entry in entries]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise EvalFixtureError(f"golden fixture {path} has duplicate case id {duplicates[0]!r}")
    return entries


def load_eval_cases(path: str | Path) -> tuple[EvalCase, ...]:
    """Load cases from a golden fixture, sorted by case id."""
    return tuple(sorted((entry.case for entry in load_golden_entries(path)), key=lambda case: case.id))


def load_recorded_outcomes(path: str | Path) -> dict[str, EvalOutcome]:
    """Load the recorded outcomes of a golden fixture keyed by case id."""
    return {entry.case.id: entry.recorded for entry in load_golden_entries(path)}


__all__ = [
    "GoldenEntry",
    "load_eval_cases",
    "load_golden_entries",
    "load_json_lines",
    "load_recorded_outcomes",
]
