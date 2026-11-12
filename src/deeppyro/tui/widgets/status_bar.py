"""One-line status bar: session, model, cwd, tokens, context, mode, phase."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from deeppyro.tui.format import context_percent, format_tokens, short_session_id

#: Maximum characters for the model part of the status line.
MAX_MODEL_CHARS = 40
#: Maximum characters for the workspace path shown in the status line.
MAX_CWD_CHARS = 42


class StatusBar(Static):
    """Bottom status line; updated by DeepPyroApp.refresh_status."""

    def __init__(self) -> None:
        super().__init__("", id="status-bar")

    def show(
        self,
        *,
        session_id: str,
        provider: str,
        model: str,
        cwd: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        context_window: int,
        approval: str,
        phase: str,
    ) -> None:
        line = Text()
        line.append("session ", style="dim")
        line.append(short_session_id(session_id), style="bold #7aa2f7")
        line.append(" · ", style="dim")
        line.append(_short_model(provider, model), style="bold #7dcfff")
        line.append(" · ", style="dim")
        line.append(short_cwd(cwd), style="dim")
        line.append("  ↑", style="dim")
        line.append(format_tokens(input_tokens), style="#9ece6a")
        line.append(" ↓", style="dim")
        line.append(format_tokens(output_tokens), style="#9ece6a")
        if cached_tokens:
            line.append(" ⚡", style="dim")
            line.append(format_tokens(cached_tokens), style="#9ece6a")
        pct = context_percent(input_tokens, context_window)
        line.append(" · ctx ", style="dim")
        line.append(f"{pct}%", style="#e0af68" if pct >= 75 else "dim")
        line.append(" · ", style="dim")
        line.append(approval, style="#e0af68" if approval == "ask" else "dim")
        line.append(" · ", style="dim")
        line.append(phase, style="#7aa2f7" if phase != "idle" else "dim")
        self.update(line)


def _short_model(provider: str, model: str, *, max_chars: int = MAX_MODEL_CHARS) -> str:
    label = f"{provider}/{model}" if provider else str(model or "")
    if len(label) <= max_chars:
        return label
    keep = max(8, max_chars - len(provider) - 2)
    return f"{provider}/{str(model)[:keep]}…"


def short_cwd(cwd: str, *, max_chars: int = MAX_CWD_CHARS) -> str:
    """Keep a long workspace path readable by showing its last two segments."""
    value = str(cwd or "")
    if len(value) <= max_chars:
        return value
    separator = "\\" if "\\" in value else "/"
    parts = [part for part in value.replace("\\", "/").strip("/").split("/") if part]
    if len(parts) >= 2:
        tail = "…" + separator + separator.join(parts[-2:])
        if len(tail) <= max_chars:
            return tail
    return "…" + value[-(max_chars - 1) :]


__all__ = ["StatusBar", "short_cwd"]
