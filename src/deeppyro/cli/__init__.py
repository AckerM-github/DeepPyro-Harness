"""The DeepPyro Harness terminal front end.

One presentation implementation serves every command: `run` and `chat` share
the same event sinks, table/config rendering and exit-code mapping. The package
is import-safe without any optional workstream being present because session,
llm, tools and agent imports are deferred into `build_app`/command bodies.
"""

from deeppyro.cli.frontend import (
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    TerminalFrontend,
)
from deeppyro.cli.main import app
from deeppyro.cli.render import JsonLinesSink, RichSink, render_session_table, short_session_id

__all__ = [
    "EXIT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_USAGE",
    "JsonLinesSink",
    "RichSink",
    "TerminalFrontend",
    "app",
    "render_session_table",
    "short_session_id",
]
