"""Tests for the slash-command registry, parser and dispatcher."""

from __future__ import annotations

import inspect

from deeppyro.api import CommandSpec
from deeppyro.tui.commands import (
    COMMANDS,
    HELP_TEXT,
    candidates,
    dispatch,
    help_text,
    live_commands,
    parse,
)

EXPECTED_NAMES = [
    "help",
    "new",
    "resume",
    "provider",
    "model",
    "preset",
    "policy",
    "approval",
    "cwd",
    "status",
    "plan",
    "jobs",
    "team",
    "thinking",
    "hooks",
    "compact",
    "clear",
    "plugins",
    "panel",
    "exit",
]


class FakeApp:
    """Records every command handler call instead of touching a runtime."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def show_help(self) -> None:
        self.calls.append(("help", ""))

    async def new_session(self) -> None:
        self.calls.append(("new", ""))

    async def resume(self, argument: str) -> None:
        self.calls.append(("resume", argument))

    async def provider_command(self, argument: str) -> None:
        self.calls.append(("provider", argument))

    async def model_command(self, argument: str) -> None:
        self.calls.append(("model", argument))

    async def preset_command(self, argument: str) -> None:
        self.calls.append(("preset", argument))

    def show_status(self) -> None:
        self.calls.append(("status", ""))

    def plan_command(self) -> None:
        self.calls.append(("plan", ""))

    async def jobs_command(self, argument: str) -> None:
        self.calls.append(("jobs", argument))

    async def team_command(self, argument: str) -> None:
        self.calls.append(("team", argument))

    async def thinking_command(self, argument: str) -> None:
        self.calls.append(("thinking", argument))

    async def compact(self) -> None:
        self.calls.append(("compact", ""))

    def clear_conversation(self) -> None:
        self.calls.append(("clear", ""))

    async def show_plugins(self) -> None:
        self.calls.append(("plugins", ""))

    def exit(self) -> None:
        self.calls.append(("exit", ""))


def test_registry_keeps_names_order_and_metadata() -> None:
    assert [command.name for command in COMMANDS] == EXPECTED_NAMES
    for command in COMMANDS:
        assert command.usage.startswith("/")
        assert command.summary
        assert isinstance(command.aliases, tuple)
        assert callable(command.handler)


def test_registry_aliases() -> None:
    by_name = {command.name: command for command in COMMANDS}
    assert by_name["help"].aliases == ("?", "h")
    assert by_name["exit"].aliases == ("quit", "q")
    assert by_name["plugins"].aliases == ()


def test_handlers_are_awaitable_coroutines() -> None:
    for command in COMMANDS:
        assert inspect.iscoroutinefunction(command.handler)


def test_help_text_covers_every_command_in_order() -> None:
    lines = HELP_TEXT.splitlines()
    assert lines[0] == "Commands"
    assert lines[-1] == "Anything else is sent to the model."
    command_lines = lines[1:-1]
    assert len(command_lines) == len(COMMANDS)
    for line, command in zip(command_lines, COMMANDS, strict=True):
        assert line == "  " + command.usage.ljust(18) + " " + command.summary
    assert lines[1] == "  /help" + " " * 14 + "show this help"


def test_parse_splits_name_and_argument() -> None:
    help_command, _ = parse("/help")
    assert help_command is not None
    assert help_command.name == "help"
    command, argument = parse("/resume abc def")
    assert command is not None and command.name == "resume"
    assert argument == "abc def"
    command, argument = parse("/model")
    assert command is not None and command.name == "model"
    assert argument == ""
    command, argument = parse("  /new  ")
    assert command is not None and command.name == "new"
    assert argument == ""


def test_parse_accepts_names_without_a_slash() -> None:
    command, _ = parse("help")
    assert command is not None and command.name == "help"
    command, _ = parse("QUIT")
    assert command is not None and command.name == "exit"


def test_parse_returns_none_for_unknown_or_blank() -> None:
    assert parse("/nope") == (None, "")
    assert parse("") == (None, "")
    assert parse("   ") == (None, "")
    assert parse("/") == (None, "")


def test_candidates_filter_by_name_and_alias() -> None:
    assert candidates("") == COMMANDS
    assert candidates("/") == COMMANDS
    assert [command.name for command in candidates("he")] == ["help"]
    assert [command.name for command in candidates("h")] == ["help", "hooks"]
    assert [command.name for command in candidates("?")] == ["help"]
    assert [command.name for command in candidates("q")] == ["exit"]
    assert [command.name for command in candidates("ex")] == ["exit"]
    assert candidates("zzz") == []


async def test_dispatch_routes_every_command() -> None:
    app = FakeApp()
    lines = [
        "/help",
        "/new",
        "/resume session-1",
        "/model fake-2",
        "/preset reviewer",
        "/status",
        "/compact",
        "/clear",
        "/plugins",
        "/exit",
    ]
    for line in lines:
        assert await dispatch(app, line) is True
    assert app.calls == [
        ("help", ""),
        ("new", ""),
        ("resume", "session-1"),
        ("model", "fake-2"),
        ("preset", "reviewer"),
        ("status", ""),
        ("compact", ""),
        ("clear", ""),
        ("plugins", ""),
        ("exit", ""),
    ]


async def test_dispatch_honours_aliases() -> None:
    app = FakeApp()
    for line in ("/?", "/h"):
        assert await dispatch(app, line) is True
    for line in ("/q", "/quit"):
        assert await dispatch(app, line) is True
    assert app.calls == [
        ("help", ""),
        ("help", ""),
        ("exit", ""),
        ("exit", ""),
    ]


async def test_dispatch_plugins_forwards_to_the_app() -> None:
    app = FakeApp()
    assert await dispatch(app, "/plugins") is True
    assert app.calls == [("plugins", "")]


class FakeRuntime:
    """Only the attribute the TUI probes for a commands service."""

    def __init__(self, commands: FakeCommandsService) -> None:
        self.commands = commands


class FakeCommandsService:
    def __init__(self, specs: list[CommandSpec]) -> None:
        self._specs = specs

    def all(self) -> list[CommandSpec]:
        return list(self._specs)


class PluginApp(FakeApp):
    def __init__(self, specs: list[CommandSpec]) -> None:
        super().__init__()
        self.runtime = FakeRuntime(FakeCommandsService(specs))


async def test_plugin_commands_join_the_live_registry() -> None:
    async def deploy(host: FakeApp, argument: str) -> None:
        host.calls.append(("deploy", argument))

    specs = [
        CommandSpec(
            name="deploy",
            summary="ship it",
            usage="/deploy [env]",
            aliases=("d",),
            handler=deploy,
        )
    ]
    app = PluginApp(specs)
    listed = live_commands(app)
    assert listed is not None
    assert [command.name for command in listed] == ["deploy"]
    assert [command.name for command in candidates("de", listed)] == ["deploy"]
    assert [command.name for command in candidates("d", listed)] == ["deploy"]
    assert "/deploy [env]" in help_text(listed)
    assert await dispatch(app, "/deploy prod") is True
    assert app.calls == [("deploy", "prod")]


def test_live_commands_without_a_runtime_falls_back_to_builtins() -> None:
    assert live_commands(FakeApp()) is None
    assert [command.name for command in candidates("he", live_commands(FakeApp()))] == ["help"]


async def test_dispatch_unknown_command_does_nothing() -> None:
    app = FakeApp()
    assert await dispatch(app, "/unknown") is False
    assert await dispatch(app, "") is False
    assert await dispatch(app, "just text") is False
    assert app.calls == []
