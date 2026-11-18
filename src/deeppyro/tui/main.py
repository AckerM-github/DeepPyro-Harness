"""Console entry point for DeepPyro.

The product is one terminal UI: running dpyro with no arguments opens the
full-screen TUI. The scriptable one-shot path is preserved as
dpyro --headless (or dpyro PROMPT), implemented by deeppyro.headless.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from collections.abc import Sequence

from deeppyro.runtime_env import INTERNAL_PTC_WORKER, INTERNAL_SANDBOX_RUNNER
from deeppyro.sandbox.types import APPROVALS, PRESETS


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
    parser.add_argument(
        "--preset",
        default=None,
        help="agent preset id: standard, ptc, minimal, cordis",
    )
    parser.add_argument(
        "--permission-mode",
        default=None,
        choices=PRESETS,
        help="permission preset for this run: read-only, workspace-write, danger-full-access",
    )
    parser.add_argument(
        "--approval-policy",
        default=None,
        choices=APPROVALS,
        help="approval policy for this run: ask or never",
    )
    parser.add_argument("--cwd", default=None, help="workspace directory")
    parser.add_argument("--max-steps", type=int, default=None, help="tool steps per turn")
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument(
        "--legacy-v4",
        action="store_true",
        help="run the one-shot prompt through the legacy v4 path (the native loop is the default)",
    )
    backend.add_argument(
        "--native-loop",
        action="store_true",
        help="deprecated alias: the native loop is the default (use --legacy-v4 for the v4 path)",
    )
    # Hidden: frozen-build self re-entry (see deeppyro.runtime_env).  REMAINDER
    # passes the payload -- runner options, or nothing for the PTC worker --
    # through untouched.  They are dispatched before argparse in most cases.
    parser.add_argument(INTERNAL_SANDBOX_RUNNER, nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    parser.add_argument(INTERNAL_PTC_WORKER, nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("deeppyro-harness")
    except Exception:  # noqa: BLE001 - version is best effort
        return "0.0.0"


def _dispatch_internal(raw: Sequence[str]) -> int | None:
    """Run one hidden re-entry flag without booting the runtime, or return None.

    These flags are the frozen-build subprocess protocol: a frozen executable
    has no interpreter and no ``.py`` files on disk, so it cannot re-invoke
    ``python -m deeppyro...`` and re-enters itself instead.  The branch runs
    before argparse and before any runtime import, so a worker sees raw stdio
    and nothing else (source installs keep the ``-m`` paths).
    """
    if not raw:
        return None
    head = raw[0]
    if head == INTERNAL_SANDBOX_RUNNER:
        from deeppyro.sandbox.windows_acl_runner import main as runner_main

        return runner_main(raw[1:])
    if head == INTERNAL_PTC_WORKER:
        from deeppyro.ptc.worker import main as worker_main

        return worker_main()
    return None


def _parsed_internal(args: argparse.Namespace) -> list[str] | None:
    """Rebuild an internal argv when argparse (not the early dispatch) saw the flag."""
    remainder = getattr(args, "internal_sandbox_runner", None)
    if remainder is not None:
        return [INTERNAL_SANDBOX_RUNNER, *remainder]
    remainder = getattr(args, "internal_ptc_worker", None)
    if remainder is not None:
        return [INTERNAL_PTC_WORKER, *remainder]
    return None


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    internal = _dispatch_internal(raw)
    if internal is not None:
        return internal
    # `dpyro plugin ...` is a management subcommand rather than a prompt: it
    # must not boot the runtime. Everything else keeps the flat CLI shape.
    if raw and raw[0] == "plugin":
        from deeppyro.plugins.cli import main as plugin_main

        return plugin_main(raw[1:])
    # `dpyro migrate ...` is likewise a management subcommand; it converts
    # session logs on disk and must not boot the runtime or a model provider.
    if raw and raw[0] == "migrate":
        from deeppyro.session.migrate_cli import main as migrate_main

        return migrate_main(raw[1:])
    parser = build_parser()
    args = parser.parse_args(raw)
    internal = _parsed_internal(args)
    if internal is not None:
        return _dispatch_internal(internal) or 0
    if args.version:
        print(f"deeppyro-harness {_version()}")
        return 0

    from deeppyro.paths import migrate_legacy_layout

    migrate_legacy_layout()

    # Backend selection: the native loop is the default.  --legacy-v4 pins the
    # frozen v4 path (KI-010); --native-loop is a deprecated alias that pins
    # native explicitly, kept until the v4 path is retired.
    native_loop: bool | None = None
    if args.native_loop:
        warnings.warn(
            "--native-loop is deprecated: the native loop is the headless default; "
            "use --legacy-v4 to run the legacy v4 path",
            DeprecationWarning,
            stacklevel=2,
        )
        print(
            "warning: --native-loop is deprecated; the native loop is the default. "
            "Use --legacy-v4 for the legacy v4 path.",
            file=sys.stderr,
        )
        native_loop = True
    elif args.legacy_v4:
        native_loop = False

    headless = bool(args.headless or args.prompt)
    if not headless:
        if native_loop is not None:
            print(
                "note: the TUI still runs the v4 path; --legacy-v4/--native-loop only affect --headless",
                file=sys.stderr,
            )
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
        preset=args.preset,
        permission_mode=args.permission_mode,
        approval_policy=args.approval_policy,
        json_output=bool(args.json),
        cwd=args.cwd,
        max_steps=args.max_steps,
        native_loop=native_loop,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
