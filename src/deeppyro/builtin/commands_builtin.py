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
        "open a previous session (no argument lists or filters them)",
        "/resume [id|index|text]",
        "resume",
        True,
    ),
    (
        "sessions",
        (),
        "list sessions with titles, times and previews",
        "/sessions [query|--all]",
        "sessions_command",
        True,
    ),
    ("rename", (), "rename the current session", "/rename <title>", "rename_command", True),
    (
        "archive",
        (),
        "archive the current session and start a new one",
        "/archive",
        "archive_command",
        False,
    ),
    (
        "unarchive",
        (),
        "restore a session archived with /archive (no argument lists them)",
        "/unarchive [index|id|text]",
        "unarchive_command",
        True,
    ),
    (
        "provider",
        (),
        "show or switch the LLM provider",
        "/provider [name]",
        "provider_command",
        True,
    ),
    (
        "model",
        (),
        "show or switch the model",
        "/model [id] or /model <provider>/<id>",
        "model_command",
        True,
    ),
    (
        "preset",
        (),
        "show or switch the agent preset",
        "/preset [id]",
        "preset_command",
        True,
    ),
    (
        "permission",
        (),
        "show or switch the sandbox and approval preset",
        "/permission [read-only|workspace-write|danger-full-access]",
        "permission_command",
        True,
    ),
    ("cwd", ("workspace",), "show or switch the workspace", "/cwd [path]", "cwd_command", True),
    ("status", (), "show session and runtime details", "/status", "show_status", False),
    ("plan", (), "show the current todo plan", "/plan", "plan_command", False),
    (
        "goal",
        (),
        "show or manage the current goal",
        "/goal [<objective>|clear|edit <objective>|pause|resume|complete]",
        "goal_command",
        True,
    ),
    ("jobs", (), "list background jobs", "/jobs", "jobs_command", False),
    (
        "team",
        (),
        "enter/leave team mode or show the agent team",
        "/team [on|off|board]",
        "team_command",
        True,
    ),
    (
        "thinking",
        (),
        "view the full reasoning of recent turns",
        "/thinking [turns]",
        "thinking_command",
        True,
    ),
    ("hooks", (), "show recent hook decisions", "/hooks", "hooks_command", False),
    ("compact", (), "compact the conversation now", "/compact", "compact", False),
    ("clear", (), "clear the conversation view", "/clear", "clear_conversation", False),
    ("plugins", (), "list loaded plugins", "/plugins", "show_plugins", False),
    ("panel", (), "open a plugin panel", "/panel [id]", "panel_command", True),
    ("settings", (), "open a plugin settings page", "/settings [id]", "settings_command", True),
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
    version = "0.2.0"
    description = "built-in slash commands"

    def setup(self) -> None:
        for spec in core_command_specs():
            self.ctx.register_command(
                spec.name,
                spec.handler,
                summary=spec.summary,
                usage=spec.usage,
                aliases=spec.aliases,
            )


__all__ = ["CommandsBuiltinPlugin", "core_command_specs"]
