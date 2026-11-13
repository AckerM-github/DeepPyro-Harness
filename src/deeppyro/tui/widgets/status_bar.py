"""One-line status bar: session, model, tokens, context, policy, and phase.

The permission preset and approval policy are shown here because they are
switched with the /policy and /approval slash commands; the workspace path is
shown by /status and /cwd.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from deeppyro.tui.format import context_percent, format_tokens, short_session_id

#: Maximum characters for the model part of the status line.
MAX_MODEL_CHARS = 40


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
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
        context_window: int,
        phase: str,
        preset: str = "",
        approval: str = "",
        context_tokens: int | None = None,
    ) -> None:
        line = Text()
        line.append("session ", style="dim")
        line.append(short_session_id(session_id), style="bold #7aa2f7")
        line.append(" · ", style="dim")
        line.append(_short_model(provider, model), style="bold #7dcfff")
        line.append("  ↑", style="dim")
        line.append(format_tokens(input_tokens), style="#9ece6a")
        line.append(" ↓", style="dim")
        line.append(format_tokens(output_tokens), style="#9ece6a")
        if cached_tokens:
            line.append(" ⚡", style="dim")
            line.append(format_tokens(cached_tokens), style="#9ece6a")
        pct = context_percent(input_tokens if context_tokens is None else context_tokens, context_window)
        line.append(" · ctx ", style="dim")
        line.append(f"{pct}%", style="#e0af68" if pct >= 75 else "dim")
        if preset or approval:
            line.append(" · ", style="dim")
            line.append(f"{preset}/{approval}", style="#bb9af7")
        line.append(" · ", style="dim")
        line.append(phase, style="#7aa2f7" if phase != "idle" else "dim")
        self.update(line)


def _short_model(provider: str, model: str, *, max_chars: int = MAX_MODEL_CHARS) -> str:
    label = f"{provider}/{model}" if provider else str(model or "")
    if len(label) <= max_chars:
        return label
    keep = max(8, max_chars - len(provider) - 2)
    return f"{provider}/{str(model)[:keep]}…"


__all__ = ["StatusBar"]
