"""dpyro migrate command line front end.

The command only parses arguments, resolves the two session roots and prints a
summary; every conversion and filesystem effect lives in deeppyro.migrate so
the same behaviour is available to the TUI and to plugins through the
session_migrate service.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deeppyro.migrate import (
    EXIT_FAILURE,
    MigrationError,
    MigrationReport,
    SessionFormat,
    run_migration,
)

__all__ = ["build_parser", "main"]


def build_parser(prog: str = "dpyro migrate") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Convert session logs between the v4 format and native schema 1.",
    )
    parser.add_argument(
        "--to",
        choices=[str(SessionFormat.NATIVE), str(SessionFormat.V4)],
        required=True,
        help="target session format",
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--session", metavar="ID", help="migrate one session by id")
    selection.add_argument("--all", action="store_true", help="migrate every discoverable session")
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    parser.add_argument(
        "--backup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep a .bak copy of overwritten targets (default: yes)",
    )
    parser.add_argument("--yes", action="store_true", help="overwrite targets that already exist")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--native-dir",
        type=Path,
        default=None,
        help="native session directory (default: deeppyro sessions dir)",
    )
    parser.add_argument(
        "--v4-dir",
        type=Path,
        default=None,
        help="v4 session directory (default: configured session dir)",
    )
    return parser


def _print_report(report: MigrationReport, *, stream: Any = None) -> None:
    out = stream if stream is not None else sys.stdout
    for entry in report.entries:
        source = str(entry.source) if entry.source is not None else "-"
        target = str(entry.target) if entry.target is not None else "-"
        line = f"{entry.status.value:<10} {entry.session_id or '-':<40} {source} -> {target}"
        if entry.reason:
            line += f" ({entry.reason})"
        print(line, file=out)
        if entry.backup is not None:
            print(f"           backup: {entry.backup}", file=out)
        for warning in entry.warnings:
            print(f"           warning: {warning}", file=out)
    prefix = "dry-run: " if report.dry_run else ""
    print(
        f"{prefix}converted={report.converted} planned={report.planned} "
        f"up-to-date={report.up_to_date} skipped={report.skipped} failed={report.failed}",
        file=out,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        report = run_migration(
            to=args.to,
            session=args.session,
            all=bool(args.all),
            dry_run=bool(args.dry_run),
            backup=bool(args.backup),
            yes=bool(args.yes),
            native_root=args.native_dir,
            v4_root=args.v4_dir,
        )
    except MigrationError as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"dpyro migrate: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=str))
    else:
        _print_report(report)
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
