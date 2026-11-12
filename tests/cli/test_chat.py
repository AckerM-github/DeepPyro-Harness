"""dpyro chat: interactive loop, slash commands, Ctrl+C interruption, resume."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from typer.testing import CliRunner

from deeppyro.cli import chat as chat_module
from deeppyro.cli import main as cli_main
from deeppyro.cli.chat import TurnInterrupts, chat_loop
from deeppyro.cli.frontend import EXIT_OK, EXIT_USAGE
from deeppyro.core.errors import DeeppyroError, SessionError
from tests.cli.fakes import (
    AsyncFakeAgent,
    FakeAgent,
    FakeInfo,
    FakeSession,
    FakeStore,
    LineReader,
    make_terminal_frontend,
)


class ChatHarness:
    """Run `chat_loop` with fake sessions/agents and capture rendered output."""

    def __init__(
        self,
        lines: list[str],
        *,
        events: list[dict[str, Any]] | None = None,
        agent_exc: Exception | None = None,
        keyboard_interrupt: bool = False,
        store: FakeStore | None = None,
        max_turns: int | None = None,
        async_agent: bool = False,
    ) -> None:
        self.frontend, self.out, self.err = make_terminal_frontend()
        self.sink = self.frontend.sink(markdown_final=True)
        self.store = store or FakeStore()
        self.session = FakeSession()
        self.events = events
        self.agent_exc = agent_exc
        self.keyboard_interrupt = keyboard_interrupt
        self.async_agent = async_agent
        self.agents: list[FakeAgent] = []
        self.reader = LineReader(lines)
        self.code = asyncio.run(
            chat_loop(
                self.frontend,
                self.store,
                self.session,
                self._create_agent,
                sink=self.sink,
                new_session=lambda: self.store.create("D:/fake/workspace"),
                interrupt_requests=None,
                read_line=self.reader,
                max_turns=max_turns,
            )
        )

    def _create_agent(self, session: Any) -> FakeAgent:
        agent_class = AsyncFakeAgent if self.async_agent else FakeAgent
        agent = agent_class(
            session,
            self.sink,
            events=self.events,
            exc=self.agent_exc,
            keyboard_interrupt=self.keyboard_interrupt,
        )
        self.agents.append(agent)
        return agent

    @property
    def text(self) -> str:
        return self.out.getvalue()

    @property
    def error_text(self) -> str:
        return self.err.getvalue()


def test_exit_command_returns_zero() -> None:
    harness = ChatHarness(["/exit"])
    assert harness.code == EXIT_OK
    assert "goodbye" in harness.text


def test_eof_returns_zero() -> None:
    harness = ChatHarness([])
    assert harness.code == EXIT_OK
    assert "goodbye" in harness.text


def test_blank_lines_are_ignored() -> None:
    harness = ChatHarness(["", "   ", "/exit"])
    assert harness.code == EXIT_OK
    assert harness.agents[0].sent == []


def test_plain_text_is_sent_to_agent() -> None:
    harness = ChatHarness(["hello there", "/exit"])
    assert harness.agents[0].sent == ["hello there"]
    assert "hello" in harness.text


def test_hello_quit_alias() -> None:
    harness = ChatHarness(["/quit"])
    assert harness.code == EXIT_OK


def test_unknown_command_warns_and_continues() -> None:
    harness = ChatHarness(["/bogus", "/exit"])
    assert harness.code == EXIT_OK
    assert "unknown command" in harness.error_text


def test_help_command() -> None:
    harness = ChatHarness(["/help", "/exit"])
    assert "commands:" in harness.text


def test_new_command_swaps_session_and_agent() -> None:
    harness = ChatHarness(["hi", "/new", "again", "/exit"])
    assert len(harness.agents) == 2
    assert harness.agents[0].session.session_id != harness.agents[1].session.session_id
    assert harness.agents[0].sent == ["hi"]
    assert harness.agents[1].sent == ["again"]
    assert "new session" in harness.text


def test_sessions_command_lists_store() -> None:
    store = FakeStore([FakeInfo("session-abcdefgh-0000-0000-0000-000000000000", "listed task", 2)])
    harness = ChatHarness(["/sessions", "/exit"], store=store)
    assert "listed task" in harness.text


def test_agent_error_keeps_chat_alive() -> None:
    harness = ChatHarness(["hi", "/exit"], agent_exc=DeeppyroError("model exploded"))
    assert harness.code == EXIT_OK
    assert "model exploded" in harness.error_text


def test_keyboard_interrupt_interrupts_turn_and_continues() -> None:
    harness = ChatHarness(["hi", "/exit"], keyboard_interrupt=True)
    assert harness.code == EXIT_OK
    assert harness.agents[0].interrupted is True
    assert "interrupted" in harness.error_text


def test_max_turns_stops_loop() -> None:
    harness = ChatHarness(["one", "two", "/exit"], max_turns=1)
    assert harness.code == EXIT_OK
    assert harness.agents[0].sent == ["one"]


def test_turn_usage_printed() -> None:
    harness = ChatHarness(["hi", "/exit"])
    assert "tokens in=10 out=5 cached=2" in harness.text


# --------------------------------------------------------------------------
# TurnInterrupts controller
# --------------------------------------------------------------------------


def test_turn_interrupts_drains_async_interrupt() -> None:
    controller = TurnInterrupts()
    agent = AsyncFakeAgent(FakeSession(), object())

    async def scenario() -> None:
        controller.arm(agent)
        controller.press()
        await controller.drain()

    asyncio.run(scenario())
    assert controller.fired is True
    assert agent.interrupted is True


def test_async_agent_chat_turn() -> None:
    harness = ChatHarness(["hi", "/exit"], async_agent=True)
    assert harness.code == EXIT_OK
    assert harness.agents[0].sent == ["hi"]
    assert "tokens in=10 out=5 cached=2" in harness.text


def test_turn_interrupts_forwards_to_armed_agent() -> None:
    controller = TurnInterrupts()
    agent = FakeAgent(FakeSession(), object())
    controller.arm(agent)
    controller.press()
    assert agent.interrupted is True
    assert controller.fired is True
    controller.press()
    assert agent.interrupted is True  # second press is a no-op


def test_turn_interrupts_idle_press_is_safe() -> None:
    controller = TurnInterrupts()
    controller.press()
    assert controller.fired is True


def test_turn_interrupts_disarm_blocks_forwarding() -> None:
    controller = TurnInterrupts()
    agent = FakeAgent(FakeSession(), object())
    controller.arm(agent)
    controller.disarm()
    controller.press()
    assert agent.interrupted is False


def test_arm_clears_previous_press() -> None:
    controller = TurnInterrupts()
    agent = FakeAgent(FakeSession(), object())
    controller.press()
    controller.arm(agent)
    assert controller.fired is False


# --------------------------------------------------------------------------
# chat command wiring
# --------------------------------------------------------------------------


def test_chat_command_prints_header_and_exits(
    runner: CliRunner, fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_module, "_console_read_line", lambda frontend: "/exit")
    result = runner.invoke(cli_main.app, ["chat"])
    assert result.exit_code == EXIT_OK
    assert "commands:" in result.stdout
    assert "goodbye" in result.stdout


def test_chat_resume_unknown_session_is_usage_error(
    runner: CliRunner, fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_module, "_console_read_line", lambda frontend: "/exit")
    result = runner.invoke(cli_main.app, ["chat", "--resume", "session-missing"])
    assert result.exit_code == EXIT_USAGE
    assert "unknown session" in result.stderr


def test_chat_resume_existing_session(runner: CliRunner, fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession("session-abcdef12-0000-0000-0000-000000000000")
    fakes.app.store.sessions.append(session)
    monkeypatch.setattr(chat_module, "_console_read_line", lambda frontend: "/exit")
    result = runner.invoke(cli_main.app, ["chat", "--resume", session.session_id])
    assert result.exit_code == EXIT_OK
    assert fakes.app.store.opened == [session.session_id]


def test_chat_build_error_is_usage(runner: CliRunner, fakes) -> None:
    fakes.build_error = DeeppyroError("cannot start")
    # DeeppyroError is a runtime error, not usage
    result = runner.invoke(cli_main.app, ["chat"])
    assert result.exit_code == 1
    assert "cannot start" in result.stderr


def test_chat_open_session_error_maps_to_error(
    runner: CliRunner, fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(session_id: str) -> FakeSession:
        raise SessionError("disk unreadable")

    monkeypatch.setattr(fakes.app, "open_session", explode)
    result = runner.invoke(cli_main.app, ["chat", "--resume", "session-x"])
    assert result.exit_code == EXIT_USAGE
    assert "unknown session" in result.stderr


def test_install_sigint_routes_to_press(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupts = TurnInterrupts()
    calls: list[tuple[int, Any]] = []
    monkeypatch.setattr(cli_main.signal, "signal", lambda sig, handler: calls.append((sig, handler)))
    monkeypatch.setattr(cli_main.signal, "getsignal", lambda sig: "previous")
    restore = cli_main._install_sigint(interrupts)
    assert restore is not None
    assert calls[0][0] == cli_main.signal.SIGINT
    calls[0][1](cli_main.signal.SIGINT, None)
    assert interrupts.fired is True
    restore()
    assert calls[-1] == (cli_main.signal.SIGINT, "previous")
