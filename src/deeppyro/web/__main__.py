"""CLI entry point: python -m deeppyro.web --port 0 --cwd PATH.

Binds the loopback web host, prints the tokenized URL and serves until Ctrl-C.
Only 127.0.0.1 (or the alias localhost) is accepted; there is no TLS and no
external-bind option by design.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from deeppyro.web.host import WebHost
from deeppyro.web.security import LOCALHOST, validate_bind_host
from deeppyro.web.version import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for the standalone web host."""
    parser = argparse.ArgumentParser(
        prog="python -m deeppyro.web",
        description="Host-only loopback web UI for the deeppyro native agent runtime.",
    )
    parser.add_argument("--host", default=LOCALHOST, help="bind address (loopback only)")
    parser.add_argument("--port", type=int, default=0, help="port (0 chooses a free loopback port)")
    parser.add_argument("--cwd", default=".", help="workspace directory the session runs in")
    parser.add_argument(
        "--session", default=None, help="resume this native session id instead of creating one"
    )
    parser.add_argument("--provider", default=None, help="provider override for a new session")
    parser.add_argument("--model", default=None, help="model override for a new session")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


async def _serve(args: argparse.Namespace) -> None:
    host = await WebHost.start(
        port=args.port,
        cwd=args.cwd,
        session_id=args.session,
        provider=args.provider,
        model=args.model,
        host=args.host,
        ui=True,
    )
    host.print_banner()
    try:
        await host.serve_forever()
    finally:
        await host.stop()


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run the host and translate Ctrl-C into exit code 130."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_bind_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
