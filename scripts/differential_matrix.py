"""Print the native synthetic coverage matrix (KI-011).

The offline CI gate prints this matrix so a capability can never silently drop
out of the native coverage set: every surface that the recorded baseline cannot
reach (images, approvals, compaction, MCP, PTC, subagents, handoff) declares a
case and a status, and the script exits non-zero when a capability has no case at
all.

Everything is read from checked-in JSON under tests/fixtures/native/synthetic;
the run is offline and deterministic and does not touch fixtures/native or node.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
for _path in (SRC_DIR, REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.native.synthetic import (  # noqa: E402
    CAPABILITIES,
    DifferentialMismatch,
    SyntheticCase,
    check_matrix,
    load_cases,
    matrix_rows,
    render_matrix,
)

EXIT_OK = 0
EXIT_FAILURE = 1


def _payload(cases: Sequence[SyntheticCase]) -> dict[str, object]:
    """Build the deterministic JSON report for CI artifacts."""
    rows = matrix_rows(cases)
    covered = sorted({row.capability.value for row in rows if row.coverage.value == "covered"})
    xfail = sorted({row.capability.value for row in rows if row.coverage.value == "xfail"})
    return {
        "passed": True,
        "capabilities": [capability.value for capability in CAPABILITIES],
        "covered": covered,
        "xfail": xfail,
        "rows": [
            {
                "case_id": row.case_id,
                "capability": row.capability.value,
                "status": row.coverage.value,
                "limitations": list(row.limitations),
            }
            for row in rows
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Load every synthetic case, print the matrix, optionally write JSON."""
    parser = argparse.ArgumentParser(description="print the native synthetic coverage matrix")
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="write a deterministic JSON report to this path",
    )
    args = parser.parse_args(argv)
    try:
        cases = load_cases()
        check_matrix(cases)
    except DifferentialMismatch as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    print(render_matrix(cases))
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(_payload(cases), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"OK: {len(cases)} native synthetic coverage cases cover KI-011")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
