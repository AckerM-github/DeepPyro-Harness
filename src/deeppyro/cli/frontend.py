"""The single terminal presentation service shared by every command.

There is exactly one front end module -- this one -- and both interaction modes
reuse the same event renderer:

* `dpyro run` streams a single turn through RichSink (plain, incremental text).
* `dpyro chat` streams turns through the same RichSink with markdown_final=True.
* `dpyro sessions` / `dpyro config show` use the table/config helpers here.

No typer and no cordis imports live in this layer, so it can later be provided
as a cordis "frontend" service while the command layer stays thin.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from rich.console import Console
from rich.text import Text

from deeppyro.cli.render import (
    JsonLinesSink,
    RichSink,
    effective_config_summary,
    render_config_summary,
    render_session_table,
    short_session_id,
)
from deeppyro.config import Config

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class TerminalFrontend:
    """Presentation facade for the terminal UI.

    One instance owns the stdout/stderr consoles for a command invocation and
    hands out the event sink that `run` and `chat` share. Keeping all
    presentation behind this object means the two modes cannot drift apart.
    """

    def __init__(
        self,
        console: Console | None = None,
        err_console: Console | None = None,
        *,
        show_reasoning: bool = True,
    ) -> None:
        self.console = console or Console(highlight=False)
        self.err_console = err_console or Console(stderr=True, highlight=False)
        self.show_reasoning = show_reasoning

    # -- event sinks -----------------------------------------------------
    def sink(self, *, json_mode: bool = False, markdown_final: bool = False) -> Any:
        """Create the EventSink for this invocation.

        `--json` gets a pure NDJSON sink; the interactive modes get a RichSink
        so `run` and `chat` render the exact same event stream.
        """
        if json_mode:
            return JsonLinesSink()
        return RichSink(
            self.console,
            markdown_final=markdown_final,
            show_reasoning=self.show_reasoning,
            err_console=self.err_console,
        )

    # -- simple output ---------------------------------------------------
    def print(self, *objects: Any, **kwargs: Any) -> None:
        """Print to the command's stdout console."""
        self.console.print(*objects, **kwargs)

    def info(self, message: str) -> None:
        """Print a neutral informational line."""
        self.console.print(Text(message))

    def dim(self, message: str) -> None:
        """Print a de-emphasised status line."""
        self.console.print(Text(message, style="dim"))

    def warning(self, message: str) -> None:
        """Print a warning line on stderr."""
        self.err_console.print(Text(message, style="yellow"))

    def error(self, message: str) -> None:
        """Print an error line on stderr (stdout stays clean for --json)."""
        self.err_console.print(Text(message, style="bold red"))

    def print_json(self, payload: Mapping[str, Any]) -> None:
        """Print one compact JSON document on stdout."""
        self.console.print(
            json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )

    # -- run header/footer -----------------------------------------------
    def banner(self, *, cwd: str, provider: str, model: str, session_id: str) -> None:
        """Print the one-line prelude shown before a non-JSON run."""
        line = Text()
        line.append("dpyro", style="bold cyan")
        line.append(f"  {provider}/{model}", style="cyan")
        line.append(f"  session {short_session_id(session_id)}", style="dim")
        line.append(f"  {cwd}", style="dim")
        self.console.print(line)

    def footer(self, sink: Any) -> None:
        """Print session id and token usage after a one-shot run."""
        session_id = getattr(sink, "session_id", None)
        parts: list[str] = []
        if session_id:
            parts.append(f"session {session_id}")
        usage = getattr(sink, "usage", None)
        if usage:
            parts.append("tokens in={input} out={output} cached={cached}".format(**usage))
        if parts:
            # One footer line even on narrow terminals; soft_wrap suppresses the
            # implicit line break so the session id and usage stay greppable.
            self.console.print(Text(" · ".join(parts), style="dim"), soft_wrap=True)

    def turn_usage(self, sink: Any) -> None:
        """Print the per-turn usage line used by the interactive chat loop."""
        usage = getattr(sink, "usage", None)
        if not usage:
            return
        self.console.print(
            Text(
                "tokens in={input} out={output} cached={cached}".format(**usage),
                style="dim",
            )
        )

    # -- chat helpers ----------------------------------------------------
    def chat_header(self, *, cwd: str, provider: str, model: str, session_id: str, resumed: bool) -> None:
        """Print the interactive preamble for a chat session."""
        self.banner(cwd=cwd, provider=provider, model=model, session_id=session_id)
        if resumed:
            self.dim("(resumed)")
        self.console.print(
            Text("commands: /exit /new /sessions · Ctrl+C interrupts the current turn", style="dim"),
            soft_wrap=True,
        )

    def chat_help(self) -> None:
        """Print the slash-command reference."""
        self.console.print(
            Text("commands: /exit /new /sessions · Ctrl+C interrupts the turn", style="dim"),
            soft_wrap=True,
        )

    # -- sessions / config ----------------------------------------------
    def session_table(self, infos: Iterable[Any], *, limit: int = 20) -> None:
        """Render the sessions list table."""
        render_session_table(self.console, infos, limit=limit)

    def config_summary(self, cfg: Config, *, path: Any = None) -> dict[str, Any]:
        """Build the effective-config view (masked secrets) for `config show`."""
        return effective_config_summary(cfg, path=path)

    def config(self, summary: Mapping[str, Any]) -> None:
        """Render the effective-config view."""
        render_config_summary(self.console, summary)
