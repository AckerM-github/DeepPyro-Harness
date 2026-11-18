"""Hand-authored differential cases for the protocol surfaces recordings cannot reach.

The frozen recordings under fixtures/native only carry real text and tool traffic,
so image/attachment, approval, compaction, MCP, PTC, subagent and handoff pairs are
synthesized under tests/fixtures/native/synthetic (KI-011). Each case is written to
a temporary directory, read back through the production parsers (read_session_file,
NativeCodec, fold, derive_messages) and compared semantically; nothing here touches
the network, the 26 MB recorded session corpus, or node.

The handoff case is xfail until a vocabulary records multi-agent handoff facts
(native schema 5.2 reserves the subagent/team events; session v4 has no handoff
record type), so the gap stays visible instead of turning green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.native.synthetic import (
    CAPABILITIES,
    DifferentialMismatch,
    MatrixRow,
    NotSupportedError,
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
def test_synthetic_case_is_semantically_equivalent(case: SyntheticCase, tmp_path: Path) -> None:
    """Native events and session v4 records must agree on the same conversation."""
    compare_case(case, tmp_path)


def test_matrix_covers_every_protocol_surface() -> None:
    """Every capability from KI-011 carries at least one case, xfail or not."""
    check_matrix(CASES)
    rows: list[MatrixRow] = matrix_rows(CASES)
    capabilities = {row.capability for row in rows}
    assert capabilities == set(CAPABILITIES)
    uncovered = {row.capability.value for row in rows if row.coverage.value == "xfail"}
    assert uncovered == {"handoff"}
    covered = {row.capability.value for row in rows} - uncovered
    assert covered == {"image", "approval", "compaction", "mcp", "ptc", "subagent"}


def test_matrix_is_printed_as_a_ci_report(capsys: pytest.CaptureFixture[str]) -> None:
    """The harness renders the covered/uncovered matrix for the CI log."""
    with capsys.disabled():
        print(render_matrix(CASES))
    report = render_matrix(CASES)
    assert report.startswith("native synthetic differential coverage (KI-011)")
    assert "covered: approval, compaction, image, mcp, ptc, subagent" in report
    assert "xfail (KI-011): handoff" in report
    assert "hand-authored" in report


def test_covered_cases_are_offline_pairs() -> None:
    """A covered case must pair both sides and must not hide metadata gaps."""
    for case in CASES:
        if case.coverage.value != "covered":
            continue
        assert case.v4 is not None, case.case_id
        assert case.native is not None, case.case_id
        assert case.xfail_reason is None
        fixture = (
            Path(__file__).resolve().parents[1] / "fixtures" / "native" / "synthetic" / f"{case.case_id}.json"
        )
        assert fixture.is_file()
        assert fixture.stat().st_size < 64 * 1024


def test_handoff_still_has_no_representation() -> None:
    """The xfail is honest: the missing vocabularies are the reason, not the fixture."""
    handoff = next(case for case in CASES if case.capability.value == "handoff")
    with pytest.raises(NotSupportedError, match="KI-011"):
        handoff.require_representation()
    reason = handoff.xfail_reason
    assert reason is not None
    assert "handoff/*" in reason or "handoff" in reason


def test_unknown_capability_is_rejected(tmp_path: Path) -> None:
    """A typo in a fixture cannot silently drop a capability from the matrix."""
    (tmp_path / "bogus.json").write_text(
        '{"case_id": "bogus", "capability": "telepathy", "coverage": "covered",'
        ' "title": "bogus", "v4": {"header": {}, "records": []},'
        ' "native": {"session_id": "x", "created_at": 0, "events": []}}',
        encoding="utf-8",
    )
    with pytest.raises(DifferentialMismatch, match="unknown capability"):
        load_cases(tmp_path)


def test_covered_case_without_native_side_is_rejected(tmp_path: Path) -> None:
    """A covered case must materialize both sides through the production parsers."""
    (tmp_path / "half.json").write_text(
        '{"case_id": "half", "capability": "image", "coverage": "covered",'
        ' "title": "half", "v4": {"header": {}, "records": []}}',
        encoding="utf-8",
    )
    with pytest.raises(DifferentialMismatch, match="covered case needs the native side"):
        load_cases(tmp_path)


def test_xfail_without_ki011_reason_is_rejected(tmp_path: Path) -> None:
    """An xfail must cite the known-issues entry so the gap is traceable."""
    (tmp_path / "quiet.json").write_text(
        '{"case_id": "quiet", "capability": "handoff", "coverage": "xfail",'
        ' "title": "quiet", "xfail_reason": "not implemented yet"}',
        encoding="utf-8",
    )
    with pytest.raises(DifferentialMismatch, match="KI-011"):
        load_cases(tmp_path)
