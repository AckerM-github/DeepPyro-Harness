"""Offline replay and golden eval driver.

The driver only reads tracked fixtures and recorded outcomes: it never imports a
provider client and never opens a network connection.  It is used by the
non-blocking native CI workflow and can be run locally with:

    python scripts/record_replay.py
    python scripts/record_replay.py replay
    python scripts/record_replay.py eval
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from deeppyro.eval import replay_fixtures, run_recorded_golden  # noqa: E402

DEFAULT_NATIVE_ROOT = REPO_ROOT / "fixtures" / "native"
DEFAULT_GOLDEN = REPO_ROOT / "tests" / "fixtures" / "eval" / "golden.jsonl"
EXIT_OK = 0
EXIT_FAILURE = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record_replay",
        description="Offline native recording replay and recorded golden evaluation.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("all", "replay", "eval"),
        default="all",
        help="which offline check to run (default: all)",
    )
    parser.add_argument(
        "--native-root",
        type=Path,
        default=DEFAULT_NATIVE_ROOT,
        help="directory containing the native manifest.json and recordings/",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN,
        help="recorded golden eval fixture (default: tests/fixtures/eval/golden.jsonl)",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        default=None,
        help="optional path for a combined deterministic JSON report",
    )
    return parser


def run_replay(native_root: Path) -> tuple[bool, dict[str, object]]:
    """Replay the native recordings and return (passed, report dict)."""
    report = replay_fixtures(native_root)
    print(f"native replay: {report.passed_count}/{report.total} cases passed")
    for line in report.failure_summary():
        print(f"  {line}")
    return report.all_passed, report.to_dict()


def run_eval(golden: Path) -> tuple[bool, dict[str, object]]:
    """Evaluate the recorded golden fixture and return (passed, report dict)."""
    report = asyncio.run(run_recorded_golden(golden))
    print(f"golden eval: {report.passed_count}/{report.total} cases passed")
    for line in report.failure_summary():
        print(f"  {line}")
    return report.all_passed, report.to_dict()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested offline checks and return the process exit code."""
    args = _build_parser().parse_args(argv)
    reports: dict[str, object] = {}
    failures: list[str] = []
    if args.command in ("all", "replay"):
        passed, report = run_replay(args.native_root)
        reports["replay"] = report
        if not passed:
            failures.append("native replay")
    if args.command in ("all", "eval"):
        passed, report = run_eval(args.golden)
        reports["eval"] = report
        if not passed:
            failures.append("golden eval")
    payload = {"passed": not failures, "failures": failures, "reports": reports}
    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote report to {args.json_path}")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return EXIT_FAILURE
    print("OK: offline replay and eval passed")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
