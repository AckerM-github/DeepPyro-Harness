"""Tests for the offline record/replay CLI driver."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from deeppyro.eval import EvalCase, EvalExpectation, EvalOutcome

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "record_replay.py"


def _load_cli() -> object:
    spec = importlib.util.spec_from_file_location("record_replay_cli", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_single_case_golden(path: Path, *, recorded_text: str) -> Path:
    case = EvalCase(
        id="case-broken",
        prompt="say the expected word",
        expect=EvalExpectation(final_text_contains=("expected",)),
    )
    recorded = EvalOutcome(final_text=recorded_text)
    path.write_text(
        json.dumps({"case": case.to_dict(), "recorded": recorded.to_dict()}) + "\n",
        encoding="utf-8",
    )
    return path


def test_cli_replay_command(capsys: object) -> None:
    cli = _load_cli()
    assert cli.main(["replay"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "native replay: 10/10 cases passed" in output
    assert "OK: offline replay and eval passed" in output


def test_cli_eval_command(capsys: object) -> None:
    cli = _load_cli()
    assert cli.main(["eval"]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "golden eval: 20/20 cases passed" in output


def test_cli_writes_combined_json_report(tmp_path: Path, capsys: object) -> None:
    cli = _load_cli()
    target = tmp_path / "report.json"
    assert cli.main(["all", "--json", str(target)]) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert f"wrote report to {target}" in output
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["passed"] is True
    assert payload["failures"] == []
    assert payload["reports"]["replay"]["all_passed"] is True
    assert payload["reports"]["eval"]["all_passed"] is True


def test_cli_fails_on_broken_golden_fixture(tmp_path: Path, capsys: object) -> None:
    cli = _load_cli()
    golden = _write_single_case_golden(tmp_path / "golden.jsonl", recorded_text="something else")
    assert cli.main(["eval", "--golden", str(golden)]) == 1
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "golden eval: 0/1 cases passed" in output
    assert "FAILED: golden eval" in output


def test_cli_fails_on_missing_native_fixtures(tmp_path: Path, capsys: object) -> None:
    cli = _load_cli()
    assert cli.main(["replay", "--native-root", str(tmp_path / "empty")]) == 1
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "native replay: 0/0 cases passed" in output
    assert "FAILED: native replay" in output
