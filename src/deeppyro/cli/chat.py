"""Interactive chat loop shared by the terminal front ends.

The loop owns conversation control only: it reads a line, dispatches slash
commands, and forwards plain text to the AgentSession while the frontend renders
the shared event stream. It has no typer or cordis imports so the same loop can
run behind a future plugin-provided frontend.

Ctrl+C policy: a TurnInterrupts controller forwards the signal to the running
AgentSession (interrupting the turn, not the process); a raised KeyboardInterrupt
is handled the same way so chat survives both signal styles.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from rich.text import Text

from deeppyro.cli.frontend import EXIT_OK, TerminalFrontend
from deeppyro.cli.render import short_session_id
from deeppyro.core.errors import DeeppyroError

EXIT_COMMANDS = frozenset({"/exit", "/quit"})
NEW_COMMAND = "/new"
SESSIONS_COMMAND = "/sessions"
HELP_COMMAND = "/help"


class Interruptible(Protocol):
    """The subset of AgentSession needed to cancel the running turn."""

    def interrupt(self) -> None: ...


class TurnInterrupts:
    """Route SIGINT/Ctrl+C to the currently running turn.

    A signal handler cannot raise from inside a worker safely, so it only calls
    `press()`: while a turn is armed an interrupt request is pushed straight
    into the AgentSession; while idle at the prompt the press is remembered so
    the loop can report that the turn was interrupted.
    """

    def __init__(self) -> None:
        self._agent: Interruptible | None = None
        self._armed = False
        self._fired = False

    @property
    def fired(self) -> bool:
        """True if a press happened since the last arm/rearm."""
        return self._fired

    def arm(self, agent: Interruptible | None) -> None:
        """Bind a turn as interruptible and clear the previous press flag."""
        self._agent = agent
        self._armed = agent is not None
        self._fired = False

    def disarm(self) -> None:
        """Stop forwarding presses to the current agent."""
        self._armed = False
        self._agent = None

    def press(self) -> None:
        """Handle one Ctrl+C: interrupt the armed turn if there is one."""
        self._fired = True
        agent, self._agent = self._agent, None
        armed, self._armed = self._armed, False
        if not armed or agent is None:
            return
        try:
            agent.interrupt()
        except Exception:  # pragma: no cover - interrupt must never kill the UI
            pass


def _console_read_line(frontend: TerminalFrontend) -> str:
    """Prompt for one line on the frontend console (overrideable in tests)."""
    frontend.console.print(Text("you", style="bold cyan"), end="")
    frontend.console.print(" > ", end="")
    return input()


def chat_loop(
    frontend: TerminalFrontend,
    store: Any,
    session: Any,
    create_agent: Callable[[Any], Any],
    *,
    sink: Any,
    new_session: Callable[[], Any],
    interrupt_requests: TurnInterrupts | None = None,
    read_line: Callable[[], str] | None = None,
    max_turns: int | None = None,
) -> int:
    """Run the interactive loop until /exit, EOF or the turn budget ends.

    `create_agent(session)` builds an AgentSession already bound to the shared
    sink, so /new only needs to swap both the session and the agent.

    Returns the process exit code: 0 for a clean goodbye.
    """
    reader = read_line or (lambda: _console_read_line(frontend))
    agent = create_agent(session)
    turns = 0
    while True:
        if max_turns is not None and turns >= max_turns:
            return EXIT_OK
        try:
            line = reader()
        except EOFError:
            frontend.dim("goodbye")
            return EXIT_OK
        text = line.strip()
        if not text:
            continue
        if text in EXIT_COMMANDS:
            frontend.dim("goodbye")
            return EXIT_OK
        if text == HELP_COMMAND:
            frontend.chat_help()
            continue
        if text == NEW_COMMAND:
            session = new_session()
            agent = create_agent(session)
            frontend.dim(f"new session {short_session_id(session.session_id)}")
            continue
        if text == SESSIONS_COMMAND:
            frontend.session_table(store.list(), limit=20)
            continue
        if text.startswith("/"):
            frontend.warning(f"unknown command: {text.split()[0]} (try /help)")
            continue

        if interrupt_requests is not None:
            interrupt_requests.arm(agent)
        sink.begin_turn()
        try:
            agent.send(text)
        except KeyboardInterrupt:
            try:
                agent.interrupt()
            except Exception:  # pragma: no cover - interrupt is best effort
                pass
            sink.end_turn()
            frontend.warning("interrupted")
            continue
        except DeeppyroError as exc:
            sink.end_turn()
            frontend.error(f"error: {exc}")
            continue
        finally:
            if interrupt_requests is not None:
                interrupt_requests.disarm()
        sink.end_turn()
        turns += 1
        frontend.turn_usage(sink)
