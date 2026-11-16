"""Type-check the tree and compare the result with the committed baseline.

pyright has no native baseline support, so this wrapper keeps one fingerprint
per diagnostic (path::rule::message) in ``pyright-baseline.json`` and fails only
on diagnostics the baseline does not account for. Shrinking the baseline is
always allowed; growing it means the reviewer accepted new type errors and the
author ran this script with ``--write``.

    python scripts/typecheck.py            # check against the baseline
    python scripts/typecheck.py --write    # rewrite the baseline (review the diff)

Fingerprints ignore line numbers so edits above a known error do not churn the
baseline, and they count duplicates so a second copy of a known error is caught.

Only ``src`` is gated: tests exercise dynamic fixtures whose shapes pyright
cannot infer, and the runtime suite already covers them. Typing the tests is a
later ratchet step, not a toolchain prerequisite.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "pyright-baseline.json"
TARGETS = ("src",)
HEADER = "Type errors accepted at the start of the pyright ratchet; shrink with scripts/typecheck.py --write."


def _pyright_command() -> list[str]:
    """Prefer the pyright installed next to the running interpreter."""
    exe = Path(sys.executable).with_name("pyright.exe" if os.name == "nt" else "pyright")
    if exe.is_file():
        return [str(exe)]
    found = shutil.which("pyright")
    if found is None:
        raise SystemExit("pyright not found: install the dev group (uv sync) first")
    return [found]


def run_pyright() -> list[dict[str, Any]]:
    """Return pyright diagnostics for the checked targets."""
    proc = subprocess.run(
        [*_pyright_command(), *TARGETS, "--outputjson"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit("pyright did not return JSON") from None
    return list(payload.get("generalDiagnostics", []))


def fingerprint(diag: dict[str, Any]) -> str:
    """Identify a diagnostic without its line number."""
    path = Path(diag["file"]).resolve().relative_to(ROOT).as_posix()
    message = " ".join(diag["message"].split())[:200]
    return f"{path}::{diag.get('rule', '?')}::{message}"


def load_baseline() -> Counter[str]:
    """Read the committed baseline as a counter of fingerprints."""
    if not BASELINE.is_file():
        raise SystemExit("pyright-baseline.json is missing: run --write to create it")
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    return Counter(data.get("errors", {}))


def write_baseline(errors: Counter[str]) -> None:
    """Write the baseline, one fingerprint per line so diffs stay readable."""
    document = {"comment": HEADER, "errors": dict(sorted(errors.items()))}
    BASELINE.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check the tree against the pyright baseline")
    parser.add_argument("--write", action="store_true", help="rewrite the baseline from this run")
    args = parser.parse_args(argv)

    current = Counter(fingerprint(diag) for diag in run_pyright() if diag.get("severity") == "error")
    if args.write:
        write_baseline(current)
        print(f"baseline written: {sum(current.values())} errors, {len(current)} distinct")
        return 0

    baseline = load_baseline()
    new = current - baseline
    fixed = baseline - current
    for key, count in sorted(new.items()):
        print(f"NEW ({count}x) {key}")
    for key, count in sorted(fixed.items()):
        print(f"FIXED ({count}x) {key}")
    print(
        f"pyright: {sum(current.values())} errors, {sum(new.values())} new, "
        f"{sum(fixed.values())} baseline entries no longer reproduce"
    )
    if new:
        print("fix the new errors, or record them with --write and justify the bump")
        return 1
    if fixed:
        print("run scripts/typecheck.py --write to record the improvement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
