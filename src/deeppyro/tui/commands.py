"""Slash-command registry and dispatch for the TUI.

Every command is a thin wrapper around a public method on the app, so the
behaviour is testable without the command-line syntax. The registry drives
dispatch(), the help text and the slash completion popup.

Unknown commands return False and the app renders an error.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

CommandHandler = Any


async def _run_help(app: Any, argument: str) -> None:
    app.show_help()


async def _run_new(app: Any, argument: str) -> None:
    await app.new_session()


async def _run_resume(app: Any, argument: str) -> None:
    await app.resume(argument)


async def _run_model(app: Any, argument: str) -> None:
    await app.model_command(argument)


async def _run_status(app: Any, argument: str) -> None:
    app.show_status()


async def _run_compact(app: Any, argument: str) -> None:
    await app.compact()


async def _run_clear(app: Any, argument: str) -> None:
    app.clear_conversation()


async def _run_plugins(app: Any, argument: str) -> None:
    await app.show_plugins()


async def _run_exit(app: Any, argument: str) -> None:
    app.exit()


@dataclass(frozen=True)
class Command:
    """One slash command: canonical name, aliases, help text and handler."""

    name: str
    aliases: tuple[str, ...]
    summary: str
    usage: str
    handler: CommandHandler

    def matches(self, token: str) -> bool:
        """True when token is the command name or one of its aliases."""
        token = token.lower()
        return token == self.name or token in self.aliases

    def matches_prefix(self, prefix: str) -> bool:
        """True when the command name or an alias starts with prefix."""
        prefix = prefix.lower()
        if not prefix:
            return True
        return self.name.startswith(prefix) or any(alias.startswith(prefix) for alias in self.aliases)


COMMANDS: list[Command] = [
    Command("help", ("?", "h"), "show this help", "/help", _run_help),
    Command("new", (), "start a new session", "/new", _run_new),
    Command(
        "resume",
        (),
        "open a previous session (no argument lists them)",
        "/resume [id|index]",
        _run_resume,
    ),
    Command("model", (), "show or switch the model", "/model [id]", _run_model),
    Command("status", (), "show session and runtime details", "/status", _run_status),
    Command("compact", (), "compact the conversation now", "/compact", _run_compact),
    Command("clear", (), "clear the conversation view", "/clear", _run_clear),
    Command("plugins", (), "list loaded plugins", "/plugins", _run_plugins),
    Command("exit", ("quit", "q"), "quit DeepPyro", "/exit", _run_exit),
]


def _command_index() -> dict[str, Command]:
    index: dict[str, Command] = {}
    for command in COMMANDS:
        index[command.name] = command
        for alias in command.aliases:
            index[alias] = command
    return index


_INDEX = _command_index()


def parse(line: str) -> tuple[Command | None, str]:
    """Split one input line into (command, argument).

    The command is None when the name is unknown. A leading slash is optional
    so the same parser works for both the prompt and direct calls.
    """
    body = line.strip().lstrip("/").strip()
    if not body:
        return None, ""
    name, _, argument = body.partition(" ")
    command = _INDEX.get(name.lower())
    return command, argument.strip()


def candidates(prefix: str) -> list[Command]:
    """Return commands matching the completion token, in registry order."""
    token = prefix.lstrip("/").strip().lower()
    return [command for command in COMMANDS if command.matches_prefix(token)]


def _build_help_text() -> str:
    lines = ["Commands"]
    for command in COMMANDS:
        lines.append("  " + command.usage.ljust(18) + " " + command.summary)
    lines.append("Anything else is sent to the model.")
    return "\n".join(lines)


HELP_TEXT = _build_help_text()


async def dispatch(app: Any, line: str) -> bool:
    """Execute one slash command; return True when the line was a command."""
    command, argument = parse(line)
    if command is None:
        return False
    result = command.handler(app, argument)
    if inspect.isawaitable(result):
        await result
    return True


__all__ = [
    "COMMANDS",
    "HELP_TEXT",
    "Command",
    "candidates",
    "dispatch",
    "parse",
]
