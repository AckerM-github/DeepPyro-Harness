"""Console entry point for DeepPyro.

The product is one terminal UI: running dpyro with no arguments opens the
full-screen TUI. The scriptable one-shot path is preserved as
dpyro --headless (or dpyro PROMPT), implemented by deeppyro.headless.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dpyro",
        description="DeepPyro Harness — terminal UI for a Python agent harness",
    )
    parser.add_argument("prompt", nargs="*", help="one-shot prompt (implies --headless)")
    parser.add_argument("--headless", action="store_true", help="run one prompt and exit")
    parser.add_argument("--json", action="store_true", help="NDJSON events instead of rich output")
    parser.add_argument("--provider", default=None, help="provider name from the config")
    parser.add_argument("--model", default=None, help="model id override")
    parser.add_argument("--cwd", default=None, help="workspace directory")
    parser.add_argument("--max-steps", type=int, default=None, help="tool steps per turn")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("deeppyro-harness")
    except Exception:  # noqa: BLE001 - version is best effort
        return "0.0.0"


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"deeppyro-harness {_version()}")
        return 0

    from deeppyro.paths import migrate_legacy_layout

    migrate_legacy_layout()

    headless = bool(args.headless or args.prompt)
    if not headless:
        from deeppyro.tui.app import DeepPyroApp

        app = DeepPyroApp(cwd=args.cwd, provider=args.provider, model=args.model)
        app.run()
        return 0

    from deeppyro.headless import read_stdin_prompt, run_once

    prompt = " ".join(args.prompt).strip() if args.prompt else read_stdin_prompt()
    if not prompt:
        print("error: empty prompt", file=sys.stderr)
        return 2
    return run_once(
        prompt,
        model=args.model,
        provider=args.provider,
        json_output=bool(args.json),
        cwd=args.cwd,
        max_steps=args.max_steps,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
