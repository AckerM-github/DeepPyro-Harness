"""Process-level launcher shared by the CLI and the TUI splash.

WebHost.start() is async and owns a kernel, an HTTP server and a session. The
TUI is synchronous once Textual exits, so this module is the one place that
turns a started host into a blocking process: print the tokenized URL, serve
until Ctrl-C, then close the host (and the kernel it booted).
"""

from __future__ import annotations

import asyncio
import sys
import webbrowser
from pathlib import Path
from typing import TextIO

from deeppyro.web.host import WebHost
from deeppyro.web.security import LOCALHOST
from deeppyro.web.version import __version__


async def serve(
    *,
    cwd: str | Path | None = None,
    session_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    port: int = 0,
    host: str = LOCALHOST,
    banner: TextIO | None = None,
    ui: bool = True,
    open_browser: bool = False,
) -> None:
    """Start a loopback host, print its URL and serve until it stops."""
    web = await WebHost.start(
        port=port,
        cwd=cwd,
        session_id=session_id,
        provider=provider,
        model=model,
        host=host,
        version=__version__,
        ui=ui,
    )
    web.print_banner(banner)
    if open_browser:
        _open_browser(web.url)
    try:
        await web.serve_forever()
    finally:
        await web.stop()


def launch(
    *,
    cwd: str | Path | None = None,
    session_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    port: int = 0,
    host: str = LOCALHOST,
    banner: TextIO | None = None,
    ui: bool = True,
    open_browser: bool = False,
) -> int:
    """Blocking entry point: 0 on a clean stop, 130 on Ctrl-C."""
    try:
        asyncio.run(
            serve(
                cwd=cwd,
                session_id=session_id,
                provider=provider,
                model=model,
                port=port,
                host=host,
                banner=banner,
                ui=ui,
                open_browser=open_browser,
            )
        )
    except KeyboardInterrupt:
        print("", file=sys.stderr if banner is None else banner)
        return 130
    return 0


def _open_browser(url: str) -> None:
    """Best-effort: open the tokenized loopback URL in the default browser."""
    if not url:
        return
    try:
        opened = webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001 - a missing browser must not stop the server
        opened = False
    if not opened:
        print(f"note: open this URL in a browser: {url}", file=sys.stderr)


__all__ = ["launch", "serve"]
