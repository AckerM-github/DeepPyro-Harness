"""Terminal rendering primitives shared by every CLI command.

There is exactly one presentation implementation in this project: RichSink
streams agent events for both `dpyro run` and `dpyro chat` (chat only flips the
markdown flag), JsonLinesSink serves `--json`, and the table/config helpers
below serve `dpyro sessions` and `dpyro config show`.

This module deliberately imports rich only -- no typer, no cordis -- so the
rendering layer stays a pure front end service that a future plugin can wrap.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from deeppyro.config import Config, config_path, session_dir
from deeppyro.core.errors import ConfigError

THINKING_MARKER = "[dim]· thinking[/]"
USAGE_KEYS = ("input", "output", "cached")


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------


def mask_secret(secret: str | None) -> str | None:
    """Mask an API key for display, keeping only a short head and tail."""
    if not secret:
        return secret
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}…{secret[-4:]}"


def compact_text(text: str, *, max_lines: int = 3, max_chars: int = 300) -> str:
    """Collapse a possibly huge payload into a short single-block preview."""
    if not text:
        return ""
    lines = text.splitlines()
    preview = "\n".join(lines[:max_lines]).strip()
    truncated = len(lines) > max_lines or len(text) > max_chars
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1] + "…"
    elif truncated:
        preview += " …"
    return preview


def format_timestamp(ms: int | None) -> str:
    """Render a DSH millisecond timestamp as local 'YYYY-MM-DD HH:MM'."""
    if not ms or ms <= 0:
        return "-"
    return _dt.datetime.fromtimestamp(ms / 1000).astimezone().strftime("%Y-%m-%d %H:%M")


def short_session_id(session_id: str | None) -> str:
    """Return the first 8 uuid characters of a session id.

    DSH ids look like `session-<uuid>`; the literal first 8 characters are always
    'session-', so the prefix is stripped before shortening.
    """
    if not session_id:
        return "-"
    bare = str(session_id).removeprefix("session-")
    return bare[:8] or "-"


def field(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from a dataclass-like object or a plain mapping."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


# --------------------------------------------------------------------------
# --json sink (NDJSON)
# --------------------------------------------------------------------------


def _json_default(value: Any) -> str:
    return str(value)


class JsonLinesSink:
    """EventSink that writes one compact JSON object per line on stdout.

    Every line is flushed immediately so scripts can consume the turn as a
    stream. Nothing else may be written to stdout in `--json` mode; errors go
    to stderr from the command layer.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream
        self.event_count = 0
        self.saw_error = False

    @property
    def stream(self) -> TextIO:
        """Destination stream, resolved lazily so CliRunner capture works."""
        return self._stream if self._stream is not None else sys.stdout

    def begin_turn(self) -> None:
        """No-op: NDJSON has no streaming block state."""

    def end_turn(self) -> None:
        """No-op: every event is already one complete line."""

    def emit(self, event: Mapping[str, Any]) -> None:
        """Serialize and flush one event as a single NDJSON line."""
        payload = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"), default=_json_default)
        out = self.stream
        out.write(payload + "\n")
        out.flush()
        self.event_count += 1
        if str(event.get("type", "")) == "error":
            self.saw_error = True


# --------------------------------------------------------------------------
# Rich event sink
# --------------------------------------------------------------------------


class RichSink:
    """Render agent events to a Rich console while they stream.

    The same class serves both interaction modes:

    * `markdown_final=False` (dpyro run): text is printed chunk by chunk.
    * `markdown_final=True` (dpyro chat): text is buffered and printed as a
      Markdown block; on a real terminal the preview is re-rendered live so the
      answer still appears to stream.

    Chunks come from the agent and may contain rich markup characters, therefore
    all streaming prints use `markup=False`.
    """

    def __init__(
        self,
        console: Console,
        *,
        markdown_final: bool = False,
        show_reasoning: bool = True,
        err_console: Console | None = None,
    ) -> None:
        self.console = console
        self.err_console = err_console or console
        self._markdown = markdown_final
        self._show_reasoning = show_reasoning
        self._mode: str | None = None
        self._text = ""
        self._final_text = ""
        self._wrote_text = False
        self._text_flushed = False
        self._tool_names: dict[str, str] = {}
        self._usage: dict[str, int] | None = None
        self._session_id: str | None = None
        self._live: Any | None = None
        self._live_updated = 0.0
        self.event_count = 0
        self.saw_error = False

    # -- read-only state -------------------------------------------------
    @property
    def session_id(self) -> str | None:
        """Last session id reported by the loop."""
        return self._session_id

    @property
    def usage(self) -> dict[str, int] | None:
        """Token usage accumulated over the current turn, if the loop sent any."""
        return dict(self._usage) if self._usage is not None else None

    @property
    def final_text(self) -> str:
        """Final assistant text of the current turn."""
        return self._final_text

    @property
    def saw_text(self) -> bool:
        """True once assistant text has been displayed (streamed or flushed)."""
        return self._wrote_text or self._text_flushed

    # -- turn lifecycle --------------------------------------------------
    def begin_turn(self) -> None:
        """Reset per-turn streaming state (chat reuses one sink for many turns)."""
        self._mode = None
        self._text = ""
        self._final_text = ""
        self._wrote_text = False
        self._text_flushed = False
        self._tool_names = {}
        self._usage = None

    def end_turn(self) -> None:
        """Flush any open block; safe to call more than once per turn."""
        if self._markdown and self._text and not self._text_flushed:
            self._flush_markdown(self._text)
            return
        self._finish_block()

    # -- EventSink -------------------------------------------------------
    def emit(self, event: Mapping[str, Any]) -> None:
        """Dispatch one agent event to its renderer."""
        self.event_count += 1
        kind = str(event.get("type", ""))
        if kind == "text":
            self._on_text(str(event.get("text") or ""))
        elif kind == "thinking":
            self._on_thinking(str(event.get("text") or ""))
        elif kind == "tool_call":
            self._on_tool_call(event)
        elif kind == "tool_result":
            self._on_tool_result(event)
        elif kind == "usage":
            self._on_usage(event)
        elif kind == "final":
            self._on_final(str(event.get("text") or ""))
        elif kind == "error":
            self._on_error(str(event.get("message") or "unknown error"))
        elif kind == "session":
            self._session_id = str(event.get("sessionId") or "") or None
        # status and unknown event kinds carry no rendering payload

    # -- event handlers --------------------------------------------------
    def _on_text(self, chunk: str) -> None:
        if not chunk:
            return
        self._start_block("text")
        self._text += chunk
        self._wrote_text = True
        if self._markdown:
            if self.console.is_terminal:
                self._update_live()
            return
        self.console.print(chunk, end="", markup=False, highlight=False)

    def _on_thinking(self, chunk: str) -> None:
        if not chunk or not self._show_reasoning:
            return
        self._start_block("thinking")
        self.console.print(chunk, end="", style="dim", markup=False, highlight=False)

    def _on_tool_call(self, event: Mapping[str, Any]) -> None:
        self._finish_block()
        name = str(event.get("name") or "tool")
        call_id = str(event.get("callId") or "")
        if call_id:
            self._tool_names[call_id] = name
        args = compact_text(str(event.get("arguments") or ""), max_lines=1, max_chars=160)
        line = Text("→ ", style="cyan")
        line.append(name, style="bold cyan")
        if args:
            line.append(" " + args, style="dim")
        self.console.print(line)

    def _on_tool_result(self, event: Mapping[str, Any]) -> None:
        self._finish_block()
        call_id = str(event.get("callId") or "")
        name = self._tool_names.get(call_id, "tool")
        is_error = bool(event.get("isError"))
        preview = compact_text(str(event.get("text") or ""))
        line = Text("  ✗ " if is_error else "  ✓ ", style="red" if is_error else "green")
        line.append(name, style="dim")
        if preview:
            line.append("  " + preview, style="dim")
        self.console.print(line)

    def _on_usage(self, event: Mapping[str, Any]) -> None:
        values = {key: int(event.get(key) or 0) for key in USAGE_KEYS}
        if self._usage is None:
            self._usage = values
            return
        for key, value in values.items():
            self._usage[key] += value

    def _on_final(self, text: str) -> None:
        self._final_text = text
        if self._markdown:
            self._flush_markdown(text or self._text)
            return
        if not self._wrote_text and text:
            self.console.print(text, markup=False, highlight=False)
            self._wrote_text = True
        self._finish_block()

    def _on_error(self, message: str) -> None:
        self.saw_error = True
        self._finish_block()
        line = Text("error: ", style="bold red")
        line.append(message, style="red")
        self.err_console.print(line)

    # -- streaming blocks ------------------------------------------------
    def _start_block(self, mode: str) -> None:
        if self._mode == mode:
            return
        self._finish_block()
        self._mode = mode
        if mode == "thinking":
            self.console.print(THINKING_MARKER)
        elif mode == "text" and self._markdown and self.console.is_terminal:
            self._start_live()

    def _finish_block(self) -> None:
        if self._mode == "text" and self._markdown:
            self._flush_markdown(self._text)
            return
        if self._mode is not None:
            self._stop_live()
            self.console.print()
        self._mode = None

    def _flush_markdown(self, text: str) -> None:
        if self._text_flushed:
            self._mode = None
            return
        self._stop_live()
        content = (text or "").strip()
        if content:
            self.console.print(Markdown(content))
        self._text_flushed = True
        self._wrote_text = self._wrote_text or bool(content)
        self._mode = None

    # -- live markdown preview -------------------------------------------
    def _start_live(self) -> None:
        try:
            from rich.live import Live

            self._live = Live(
                Markdown(""),
                console=self.console,
                refresh_per_second=10,
                transient=True,
            )
            self._live.start()
        except Exception:  # noqa: BLE001 - terminal-only best effort
            self._live = None

    def _update_live(self) -> None:
        if self._live is None:
            return
        now = time.monotonic()
        if now - self._live_updated < 0.1:
            return
        self._live_updated = now
        try:
            self._live.update(Markdown(self._text))
        except Exception:  # noqa: BLE001 - terminal-only best effort
            self._stop_live()

    def _stop_live(self) -> None:
        live, self._live = self._live, None
        if live is None:
            return
        try:
            live.stop()
        except Exception:  # noqa: BLE001, S110 - terminal-only best effort
            pass


# --------------------------------------------------------------------------
# Session table, config summary
# --------------------------------------------------------------------------


def render_session_table(console: Console, infos: Iterable[Any], *, limit: int = 20) -> None:
    """Print local sessions as a table: time, title, turn count, short id."""
    items = list(infos)[: max(limit, 0)]
    if not items:
        console.print("[dim]no sessions found[/]")
        return
    table = Table(box=None, header_style="bold", pad_edge=False, show_edge=False)
    table.add_column("时间", style="dim", no_wrap=True)
    table.add_column("标题", overflow="fold")
    table.add_column("轮数", justify="right", style="dim")
    table.add_column("id", style="cyan", no_wrap=True)
    for info in items:
        title = " ".join(str(field(info, "title") or "(untitled)").split())
        table.add_row(
            format_timestamp(field(info, "createdAt", 0)),
            title,
            str(field(info, "turns", 0)),
            short_session_id(field(info, "id")),
        )
    console.print(table)


def effective_config_summary(cfg: Config, *, path: Path | None = None) -> dict[str, Any]:
    """Build the JSON-able view of the effective config with secrets masked."""
    config_file = path or config_path()
    try:
        _, model = cfg.resolve()
    except KeyError as exc:
        raise ConfigError(str(exc.args[0])) from exc
    providers: dict[str, Any] = {}
    for name, pc in cfg.providers.items():
        resolved_key = pc.resolve_api_key()
        if pc.apiKey:
            key_source = "config"
        elif pc.apiKeyEnv and resolved_key:
            key_source = f"env:{pc.apiKeyEnv}"
        else:
            key_source = None
        providers[name] = {
            "api": pc.api,
            "baseURL": pc.baseURL,
            "apiKeyEnv": pc.apiKeyEnv,
            "apiKey": mask_secret(resolved_key),
            "apiKeySource": key_source,
            "models": [
                {"id": entry.id, "name": entry.name, "contextWindow": entry.contextWindow}
                for entry in pc.models
            ],
        }
    return {
        "configPath": str(config_file),
        "configExists": config_file.exists(),
        "sessionDir": str(session_dir(cfg)),
        "agent": {
            "provider": cfg.agent.provider,
            "model": model,
            "modelOverride": cfg.agent.model,
            "permissionPreset": cfg.agent.permissionPreset,
            "approvalPolicy": cfg.agent.approvalPolicy,
            "maxSteps": cfg.agent.maxSteps,
            "maxTokens": cfg.agent.maxTokens,
            "temperature": cfg.agent.temperature,
        },
        "providers": providers,
    }


def render_config_summary(console: Console, summary: Mapping[str, Any]) -> None:
    """Print the effective configuration: paths, agent defaults and providers.

    Providers are printed as indented key/value blocks instead of a wide table:
    URLs, key ids and model lists must never be ellipsized, even in an 80-column
    terminal (or in tests that render with a narrow console).
    """
    missing = "" if summary.get("configExists") else " [yellow](missing, using built-in defaults)[/]"
    console.print(f"[bold]config[/]: {summary.get('configPath')}{missing}", soft_wrap=True)
    console.print(f"[bold]sessions[/]: {summary.get('sessionDir')}", soft_wrap=True)
    agent = summary.get("agent") or {}
    console.print(
        "[bold]agent[/]: "
        f"provider={agent.get('provider')} model={agent.get('model')} "
        f"preset={agent.get('permissionPreset')} approval={agent.get('approvalPolicy')} "
        f"maxSteps={agent.get('maxSteps')} maxTokens={agent.get('maxTokens')} "
        f"temperature={agent.get('temperature')}",
        soft_wrap=True,
    )
    console.print("[bold]providers[/]")
    for name, pc in (summary.get("providers") or {}).items():
        key = pc.get("apiKey") or "-"
        if pc.get("apiKeySource"):
            key = f"{key} ({pc['apiKeySource']})"
        models = ", ".join(str(model.get("id")) for model in pc.get("models") or []) or "-"
        console.print(f"  [cyan]{name}[/] ({pc.get('api') or '-'})", soft_wrap=True)
        console.print(f"    baseURL  {pc.get('baseURL') or '-'}", soft_wrap=True)
        console.print(f"    apiKey   {key}", soft_wrap=True)
        console.print(f"    models   {models}", soft_wrap=True)
