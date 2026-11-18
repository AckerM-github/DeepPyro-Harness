"""Hand-authored native coverage cases for the protocol surfaces recordings cannot reach.

The frozen recordings under fixtures/native only carry real text and tool traffic, so
image/attachment, approval, compaction, MCP, PTC, subagent and handoff cases are
synthesized under tests/fixtures/native/synthetic (KI-011). Each case is materialized
into a temporary directory, read back through the production codec (NativeCodec, fold)
and asserted against the native shape invariants; nothing here touches the network,
the recorded session corpus, or node.

The handoff case is covered by schema 1 task facts: task.state, task.handoff and
task.handoff.result. Per-handoff token usage lives in the trace side channel; the
case documents that split in its limitations.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from deeppyro.session.schema import EventKind
from tests.native.synthetic import (
    CAPABILITIES,
    FIXTURE_ROOT,
    HANDOFF_NATIVE_TYPES,
    DifferentialMismatch,
    MatrixRow,
    SyntheticCase,
    check_matrix,
    compare_case,
    load_cases,
    matrix_rows,
    render_matrix,
)

CASES = load_cases()
CASE_IDS = [case.case_id for case in CASES]


def _params() -> list[object]:
    params: list[object] = []
    for case in CASES:
        if case.coverage.value == "xfail":
            reason = case.xfail_reason or "KI-011: capability has no offline representation"
            params.append(
                pytest.param(case, id=case.case_id, marks=pytest.mark.xfail(strict=True, reason=reason))
            )
        else:
            params.append(pytest.param(case, id=case.case_id))
    return params


@pytest.mark.parametrize("case", _params())
def test_synthetic_case_satisfies_native_invariants(case: SyntheticCase, tmp_path: Path) -> None:
    """A synthetic case must fold into the native message, tool and trace shapes."""
    compare_case(case, tmp_path)


def test_matrix_covers_every_protocol_surface() -> None:
    """Every capability from KI-011 carries a covered case."""
    check_matrix(CASES)
    rows: list[MatrixRow] = matrix_rows(CASES)
    capabilities = {row.capability for row in rows}
    assert capabilities == set(CAPABILITIES)
    uncovered = {row.capability.value for row in rows if row.coverage.value == "xfail"}
    assert uncovered == set()
    covered = {row.capability.value for row in rows}
    assert covered == {"image", "approval", "compaction", "mcp", "ptc", "subagent", "handoff"}


def test_matrix_is_printed_as_a_ci_report() -> None:
    """The harness renders the covered/uncovered matrix for the CI log."""
    report = render_matrix(CASES)
    assert report.startswith("native synthetic coverage matrix (KI-011)")
    assert "hand-authored" in report
    assert f"cases: {len(CASES)} hand-authored offline schema-1 sessions" in report
    assert "covered: approval, compaction, handoff, image, mcp, ptc, subagent" in report
    assert "xfail (KI-011): -" in report


def test_covered_cases_are_offline_fixtures() -> None:
    """A covered case must carry the native side and a small checked-in fixture."""
    for case in CASES:
        if case.coverage.value != "covered":
            continue
        assert case.native is not None, case.case_id
        assert case.xfail_reason is None
        fixture = FIXTURE_ROOT / f"{case.case_id}.json"
        assert fixture.is_file()
        assert fixture.stat().st_size < 64 * 1024


def test_handoff_case_is_covered_by_task_facts() -> None:
    """The handoff case is a real covered case, not an xfail hiding a gap."""
    handoff = next(case for case in CASES if case.capability.value == "handoff")
    handoff.require_representation()
    assert handoff.coverage.value == "covered"
    assert handoff.xfail_reason is None
    assert handoff.native is not None
    kinds = {kind.value for kind in EventKind}
    assert set(HANDOFF_NATIVE_TYPES) <= kinds
    raw = json.loads((FIXTURE_ROOT / f"{handoff.case_id}.json").read_text(encoding="utf-8"))
    events = raw["native"]["events"]
    types = {event.get("type") for event in events}
    assert set(HANDOFF_NATIVE_TYPES) <= types


def _write_case(tmp_path: Path, payload: Mapping[str, object]) -> Path:
    path = tmp_path / f"{payload['case_id']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_unknown_capability_is_rejected(tmp_path: Path) -> None:
    """A typo in a fixture cannot silently drop a capability from the matrix."""
    _write_case(
        tmp_path,
        {
            "case_id": "bogus",
            "capability": "telepathy",
            "coverage": "covered",
            "title": "bogus",
            "native": {"session_id": "s", "created_at": 0, "events": [{"type": "turn.started"}]},
        },
    )
    with pytest.raises(DifferentialMismatch, match="unknown capability"):
        load_cases(tmp_path)


def test_covered_case_without_native_side_is_rejected(tmp_path: Path) -> None:
    """A covered case must materialize the native side through the production codec."""
    _write_case(
        tmp_path,
        {"case_id": "half", "capability": "image", "coverage": "covered", "title": "half"},
    )
    with pytest.raises(DifferentialMismatch, match="covered case needs the native side"):
        load_cases(tmp_path)


def test_xfail_without_ki011_reason_is_rejected(tmp_path: Path) -> None:
    """An xfail must cite the known-issues entry so the gap is traceable."""
    _write_case(
        tmp_path,
        {
            "case_id": "quiet",
            "capability": "handoff",
            "coverage": "xfail",
            "title": "quiet",
            "xfail_reason": "not implemented yet",
        },
    )
    with pytest.raises(DifferentialMismatch, match="KI-011"):
        load_cases(tmp_path)


def test_stale_v4_field_is_rejected(tmp_path: Path) -> None:
    """A fixture that still carries a v4 side must fail loudly, not be ignored."""
    _write_case(
        tmp_path,
        {
            "case_id": "stale",
            "capability": "image",
            "coverage": "covered",
            "title": "stale",
            "v4": {"header": {}, "records": []},
            "native": {"session_id": "s", "created_at": 0, "events": [{"type": "turn.started"}]},
        },
    )
    with pytest.raises(DifferentialMismatch, match="unknown synthetic case field"):
        load_cases(tmp_path)
