"""Tests for offline replay of recorded native fixtures."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from deeppyro.eval import (
    ReplayCaseResult,
    ReplayCheck,
    ReplayReport,
    replay_fixtures,
    replay_recording,
)
from deeppyro.eval.errors import ReplayError

REPO_ROOT = Path(__file__).resolve().parents[2]
NATIVE = REPO_ROOT / "fixtures" / "native"


def _copy_fixtures(tmp_path: Path) -> Path:
    target = tmp_path / "native"
    shutil.copytree(NATIVE, target)
    return target


def test_replay_fixtures_passes_for_every_case() -> None:
    report = replay_fixtures(NATIVE)
    assert report.total == 10
    assert report.passed_count == 10
    assert report.failed_count == 0
    assert report.failed_case_ids == ()
    assert report.all_passed is True
    assert all(check.passed for check in report.checks)
    assert {check.name for check in report.checks} >= {"manifest", "manifest-integrity", "manifest-counts"}
    requests = sum(case.requests for case in report.cases)
    tool_calls = sum(case.tool_calls for case in report.cases)
    assert requests == 17
    assert tool_calls == 7
    assert [case.case_id for case in report.cases] == sorted(case.case_id for case in report.cases)


def test_replay_report_is_deterministic() -> None:
    first = replay_fixtures(NATIVE).to_dict()
    second = replay_fixtures(NATIVE).to_dict()
    assert first == second
    assert first["all_passed"] is True


def test_replay_single_recording() -> None:
    result = replay_recording(NATIVE / "recordings" / "06_tool_single.jsonl")
    assert result.case_id == "06_tool_single"
    assert result.file == "06_tool_single.jsonl"
    assert result.status == "ok"
    assert result.requests == 2
    assert result.tool_calls == 1
    assert result.passed is True
    assert result.failed_checks == ()
    data = result.to_dict()
    assert data["passed"] is True
    assert isinstance(data["checks"], list)


def test_replay_missing_recording_is_reported() -> None:
    result = replay_recording(NATIVE / "recordings" / "does-not-exist.jsonl")
    assert result.passed is False
    assert result.status == "error"
    assert result.checks[0].name == "jsonl"
    assert "cannot read" in result.checks[0].detail


def test_replay_malformed_recording_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "broken.jsonl"
    target.write_text("{not json}\n", encoding="utf-8")
    result = replay_recording(target)
    assert result.case_id == "broken"
    assert result.passed is False
    assert result.checks[0].passed is False
    assert result.checks[0].name == "jsonl"


def test_replay_missing_manifest_is_reported(tmp_path: Path) -> None:
    report = replay_fixtures(tmp_path)
    assert report.total == 0
    assert report.all_passed is False
    assert [check.name for check in report.checks] == ["manifest"]
    assert report.checks[0].passed is False
    assert report.failure_summary()[0].startswith("manifest: manifest:")


def test_replay_invalid_manifest_json_is_reported(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{not json}", encoding="utf-8")
    report = replay_fixtures(tmp_path)
    assert report.all_passed is False
    assert report.checks[0].name == "manifest"
    assert "invalid JSON" in report.checks[0].detail


def test_replay_detects_tampered_content(tmp_path: Path) -> None:
    root = _copy_fixtures(tmp_path)
    target = root / "recordings" / "01_text_short.jsonl"
    lines: list[str] = []
    replaced = False
    for line in target.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        content = payload.get("content")
        if payload.get("type") == "llm.response.end" and isinstance(content, str) and content:
            payload["content"] = ("X" if content[0] != "X" else "Y") + content[1:]
            replaced = True
        lines.append(json.dumps(payload, ensure_ascii=False))
    assert replaced is True
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = replay_fixtures(root)
    assert report.all_passed is False
    assert report.failed_case_ids == ("01_text_short",)
    case = next(result for result in report.cases if result.case_id == "01_text_short")
    failed = {check.name for check in case.failed_checks}
    assert "assembled-content" in failed
    manifest_failures = {check.name for check in report.checks if not check.passed}
    assert "manifest-integrity" in manifest_failures


def test_replay_case_result_status_gates_passed() -> None:
    passed = ReplayCaseResult(
        case_id="a-case",
        file="a.jsonl",
        status="ok",
        requests=1,
        tool_calls=0,
        checks=(ReplayCheck("sse-parse", True),),
    )
    assert passed.passed is True
    not_ok = ReplayCaseResult(
        case_id="a-case",
        file="a.jsonl",
        status="error",
        requests=1,
        tool_calls=0,
        checks=(ReplayCheck("sse-parse", True),),
    )
    assert not_ok.passed is False


def test_replay_report_invariants() -> None:
    a_case = ReplayCaseResult("a-case", "a.jsonl", "ok", 1, 0, (ReplayCheck("sse-parse", True),))
    b_case = ReplayCaseResult("b-case", "b.jsonl", "ok", 1, 0, (ReplayCheck("sse-parse", True),))
    report = ReplayReport(cases=(a_case, b_case), checks=(ReplayCheck("manifest", True),))
    assert report.total == 2
    assert report.all_passed is True
    with pytest.raises(ReplayError, match="sorted by case id"):
        ReplayReport(cases=(b_case, a_case))
    with pytest.raises(ReplayError, match="duplicate case ids"):
        ReplayReport(cases=(a_case, a_case))
    with pytest.raises(ReplayError, match="duplicate check names"):
        ReplayReport(checks=(ReplayCheck("manifest", True), ReplayCheck("manifest", True)))
