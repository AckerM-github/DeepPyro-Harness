"""Slash-command registry and dispatch for the TUI.

Command metadata and handlers come from the commands service, so both built-in
and plugin-contributed commands use one path. The static COMMANDS list mirrors
the built-in plugin and keeps the parser usable before a runtime is booted
(tests, early startup, completion defaults).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from deeppyro.builtin.commands_builtin import core_command_specs


@dataclass(frozen=True)
class Command:
    """One slash command: canonical name, aliases, help text and handler."""

    name: str
    aliases: tuple[str, ...]
    summary: str
    usage: str
    handler: Any

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


def _from_spec(spec: Any) -> Command:
    return Command(
        name=spec.name,
        aliases=tuple(spec.aliases),
        summary=spec.summary,
        usage=spec.usage,
        handler=spec.handler,
    )


COMMANDS: list[Command] = [_from_spec(spec) for spec in core_command_specs()]


def command_list(source: Any = None) -> list[Command]:
    """Commands exposed by a commands service, or the built-in fallback."""
    if source is None:
        return list(COMMANDS)
    return [_from_spec(spec) for spec in source.all()]


def _index(commands: list[Command] | None) -> dict[str, Command]:
    index: dict[str, Command] = {}
    for command in COMMANDS if commands is None else commands:
        index[command.name] = command
        for alias in command.aliases:
            index[alias] = command
    return index


def parse(line: str, commands: list[Command] | None = None) -> tuple[Command | None, str]:
    """Split one input line into (command, argument).

    The command is None when the name is unknown. A leading slash is optional
    so the same parser works for both the prompt and direct calls.
    """
    body = line.strip().lstrip("/").strip()
    if not body:
        return None, ""
    name, _, argument = body.partition(" ")
    command = _index(commands).get(name.lower())
    return command, argument.strip()


def candidates(prefix: str, commands: list[Command] | None = None) -> list[Command]:
    """Return commands matching the completion token, in registry order."""
    token = prefix.lstrip("/").strip().lower()
    source = COMMANDS if commands is None else commands
    return [command for command in source if command.matches_prefix(token)]


def help_text(commands: list[Command] | None = None) -> str:
    source = COMMANDS if commands is None else commands
    lines = ["Commands"]
    for command in source:
        lines.append("  " + command.usage.ljust(18) + " " + command.summary)
    lines.append("Anything else is sent to the model.")
    return "\n".join(lines)


HELP_TEXT = help_text()


def live_commands(app: Any) -> list[Command] | None:
    """Commands from the booted runtime, or None when it has no service."""
    runtime = getattr(app, "runtime", None)
    service = getattr(runtime, "commands", None) if runtime is not None else None
    if service is None:
        return None
    return command_list(service)


async def dispatch(app: Any, line: str) -> bool:
    """Execute one slash command; return True when the line was a command."""
    command, argument = parse(line, live_commands(app))
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
    "command_list",
    "dispatch",
    "help_text",
    "live_commands",
    "parse",
]
