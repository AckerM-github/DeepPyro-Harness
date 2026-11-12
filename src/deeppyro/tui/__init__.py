"""Compatibility alias for the terminal front end.

DSH ships a single terminal implementation; there is no separate TUI code base.
Everything live lives in `deeppyro.cli` (`run` and `chat` share one renderer and
one interactive loop), and this module only re-exports it for callers that
import the historical `deeppyro.tui` namespace.
"""

from deeppyro.cli.chat import TurnInterrupts, chat_loop
from deeppyro.cli.frontend import TerminalFrontend
from deeppyro.cli.render import JsonLinesSink, RichSink, render_session_table, short_session_id

__all__ = [
    "JsonLinesSink",
    "RichSink",
    "TerminalFrontend",
    "TurnInterrupts",
    "chat_loop",
    "render_session_table",
    "short_session_id",
]
