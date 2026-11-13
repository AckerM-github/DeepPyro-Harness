"""The built-in slash commands, registered on the commands service.

Command metadata lives here so the host has one source of truth: the kernel
plugin registers them on the commands service at boot, and the TUI parser reads
them back from that service. The same table drives the static fallback used
before a runtime exists (early startup, tests).
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar

from deeppyro.api import CommandSpec, Plugin

#: (name, aliases, summary, usage, host method, takes argument)
_ROWS: tuple[tuple[str, tuple[str, ...], str, str, str, bool], ...] = (
    ("help", ("?", "h"), "show this help", "/help", "show_help", False),
    ("new", (), "start a new session", "/new", "new_session", False),
    (
        "resume",
        (),
        "open a previous session (no argument lists them)",
        "/resume [id|index]",
        "resume",
        True,
    ),
    ("model", (), "show or switch the model", "/model [id]", "model_command", True),
    ("status", (), "show session and runtime details", "/status", "show_status", False),
    ("compact", (), "compact the conversation now", "/compact", "compact", False),
    ("clear", (), "clear the conversation view", "/clear", "clear_conversation", False),
    ("plugins", (), "list loaded plugins", "/plugins", "show_plugins", False),
    ("exit", ("quit", "q"), "quit DeepPyro", "/exit", "exit", False),
)


def _make_handler(method_name: str, takes_argument: bool) -> Any:
    async def handler(host: Any, argument: str) -> Any:
        method = getattr(host, method_name)
        result = method(argument) if takes_argument else method()
        if inspect.isawaitable(result):
            result = await result
        return result

    return handler


def core_command_specs() -> list[CommandSpec]:
    """Fresh specs for the built-in commands; handlers stay stateless."""
    return [
        CommandSpec(
            name=name,
            summary=summary,
            usage=usage,
            aliases=aliases,
            handler=_make_handler(method_name, takes_argument),
        )
        for name, aliases, summary, usage, method_name, takes_argument in _ROWS
    ]


class CommandsBuiltinPlugin(Plugin):
    """Registers the built-in slash commands."""

    inject: ClassVar[list[str]] = ["commands"]
    plugin_id = "commands-builtin"
    version = "0.1.0"
    description = "built-in slash commands"

    def setup(self, ctx: Any, config: Any) -> None:
        for spec in core_command_specs():
            ctx.register_command(
                spec.name,
                spec.handler,
                summary=spec.summary,
                usage=spec.usage,
                aliases=spec.aliases,
            )


__all__ = ["CommandsBuiltinPlugin", "core_command_specs"]
