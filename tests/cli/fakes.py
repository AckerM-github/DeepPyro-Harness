"""Fakes shared by the CLI test-suite (importable, no pytest dependency).

Every test runs without the session / llm / tools / agent workstreams:
`build_app`, `load_config` and `list_session_infos` are replaced by the
Installer below, and chat tests drive `chat_loop` with a fake AgentSession and
a scripted line reader. That keeps the front end verifiable in isolation while
still testing the real typer wiring through `CliRunner`.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from deeppyro.cli import main as cli_main
from deeppyro.cli.frontend import TerminalFrontend
from deeppyro.config import AgentConfig, Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import SessionError

DEFAULT_SESSION_ID = "session-11111111-2222-3333-4444-555555555555"

DEFAULT_EVENTS: list[dict[str, Any]] = [
    {"type": "status", "phase": "thinking"},
    {"type": "thinking", "text": "pondering"},
    {"type": "text", "text": "hello"},
    {"type": "usage", "input": 10, "output": 5, "cached": 2},
    {"type": "final", "text": "hello"},
]


def make_config(
    *,
    provider: str = "local",
    model: str = "local-model",
    api_key: str | None = None,
) -> Config:
    """Build a complete two-provider config, as loaded from TOML in production."""
    return Config(
        providers={
            "local": ProviderConfig(
                api="openai-completions",
                baseURL="http://127.0.0.1:8000/v1",
                apiKeyEnv="LOCAL_API_KEY",
                apiKey=api_key,
                models=[ModelConfig(id="local-model", name="Local", contextWindow=8192)],
            ),
            "deepseek": ProviderConfig(
                api="openai-completions",
                baseURL="https://api.deepseek.com/v1",
                apiKeyEnv="DEEPSEEK_API_KEY",
                models=[ModelConfig(id="deepseek-chat", name="DeepSeek", contextWindow=65536)],
            ),
        },
        agent=AgentConfig(
            provider=provider,
            model=model,
            temperature=0.2,
            maxTokens=512,
            maxSteps=7,
        ),
    )


class FakeSession:
    """Minimal session object: only `session_id` is part of the CLI contract."""

    def __init__(self, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.session_id = session_id


class FakeInfo:
    """Minimal SessionInfo stand-in for the sessions table."""

    def __init__(
        self,
        session_id: str,
        title: str | None,
        turns: int,
        created_at: int = 1_700_000_000_000,
    ) -> None:
        self.id = session_id
        self.title = title
        self.turns = turns
        self.createdAt = created_at
        self.path = f"sessions/{session_id}.jsonl"
        self.cwd = "D:/fake/workspace"
        self.recordCount = turns + 1
        self.agentPreset = None


class FakeStore:
    """In-memory SessionStore: create/open/list without touching the filesystem."""

    def __init__(self, infos: Iterable[FakeInfo] = ()) -> None:
        self.infos: list[FakeInfo] = list(infos)
        self.sessions: list[FakeSession] = []
        self.created: list[str] = []
        self.opened: list[str] = []

    def create(
        self,
        cwd: str,
        *,
        agent_preset: str | None = None,
        session_id: str | None = None,
    ) -> FakeSession:
        sid = session_id or f"session-{len(self.sessions):08d}-0000-0000-0000-000000000000"
        session = FakeSession(sid)
        self.sessions.append(session)
        self.created.append(cwd)
        self.infos.insert(0, FakeInfo(sid, None, 0))
        return session

    def open(self, session_id: str) -> FakeSession:
        for session in self.sessions:
            if session.session_id == session_id:
                self.opened.append(session_id)
                return session
        raise SessionError(f"session not found: {session_id}")

    def list(self) -> list[FakeInfo]:
        return list(self.infos)


class FakeAgent:
    """AgentSession stand-in: replays a scripted event list into the sink."""

    def __init__(
        self,
        session: FakeSession,
        sink: Any,
        *,
        events: Iterable[dict[str, Any]] | None = None,
        exc: Exception | None = None,
        keyboard_interrupt: bool = False,
    ) -> None:
        self.session = session
        self.sink = sink
        self.events = list(DEFAULT_EVENTS if events is None else events)
        self.exc = exc
        self.keyboard_interrupt = keyboard_interrupt
        self.sent: list[str] = []
        self.interrupted = False

    def _run(self, text: str) -> str:
        self.sent.append(text)
        if self.keyboard_interrupt:
            raise KeyboardInterrupt
        if self.exc is not None:
            raise self.exc
        for event in self.events:
            self.sink.emit(event)
        final = next((e.get("text", "") for e in self.events if e.get("type") == "final"), "")
        return str(final)

    def send(self, text: str) -> str:
        return self._run(text)

    def interrupt(self) -> None:
        self.interrupted = True


class AsyncFakeAgent(FakeAgent):
    """AgentSession stand-in with the real coroutine-shaped send/interrupt API."""

    async def send(self, text: str) -> str:
        return self._run(text)

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeApp:
    """CliApp stand-in returned by the patched `build_app`."""

    def __init__(
        self,
        *,
        cfg: Config | None = None,
        store: FakeStore | None = None,
        agent_events: Iterable[dict[str, Any]] | None = None,
        agent_exc: Exception | None = None,
        keyboard_interrupt: bool = False,
        async_agent: bool = False,
    ) -> None:
        self.cfg = cfg or make_config()
        self.cwd = Path("D:/fake/workspace")
        self.provider_name = self.cfg.agent.provider
        self.model = "local-model"
        self.context_window = 8192
        self.store = store or FakeStore()
        self.provider = object()
        self.registry = object()
        self.permission = object()
        self.frontend = TerminalFrontend()
        self.agent_events = agent_events
        self.agent_exc = agent_exc
        self.keyboard_interrupt = keyboard_interrupt
        self.async_agent = async_agent
        self.agents: list[FakeAgent] = []
        self.max_steps_args: list[int | None] = []
        self.closed = False

    def new_session(self) -> FakeSession:
        return self.store.create(str(self.cwd))

    def open_session(self, session_id: str) -> FakeSession:
        return self.store.open(session_id)

    def list_sessions(self, *, limit: int = 20) -> list[FakeInfo]:
        return self.store.list()[:limit]

    def create_agent(self, session: FakeSession, sink: Any, *, max_steps: int | None = None) -> FakeAgent:
        agent_class = AsyncFakeAgent if self.async_agent else FakeAgent
        agent = agent_class(
            session,
            sink,
            events=self.agent_events,
            exc=self.agent_exc,
            keyboard_interrupt=self.keyboard_interrupt,
        )
        self.agents.append(agent)
        self.max_steps_args.append(max_steps)
        return agent

    async def aclose(self) -> None:
        self.closed = True


class Installer:
    """Patches the module-level seams that `deeppyro.cli.main` resolves lazily."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.app = FakeApp()
        self.cfg = self.app.cfg
        self.build_error: Exception | None = None
        self.load_error: Exception | None = None
        self.store_error: Exception | None = None
        self.build_kwargs: dict[str, Any] = {}

    def install(self) -> Installer:
        self.monkeypatch.setattr(cli_main, "build_app", self._build_app)
        self.monkeypatch.setattr(cli_main, "load_config", self._load_config)
        self.monkeypatch.setattr(cli_main, "list_session_infos", self._list_sessions)
        return self

    def _build_app(self, **kwargs: Any) -> FakeApp:
        self.build_kwargs = kwargs
        if self.build_error is not None:
            raise self.build_error
        return self.app

    def _load_config(self, *args: Any, **kwargs: Any) -> Config:
        if self.load_error is not None:
            raise self.load_error
        return self.cfg

    def _list_sessions(self, cfg: Config | None = None) -> list[FakeInfo]:
        if self.store_error is not None:
            raise self.store_error
        return self.app.store.list()


def make_terminal_frontend() -> tuple[TerminalFrontend, io.StringIO, io.StringIO]:
    """Frontend writing to StringIO with ANSI disabled for deterministic asserts."""
    out, err = io.StringIO(), io.StringIO()
    console = Console(file=out, width=100, force_terminal=False, no_color=True, highlight=False)
    err_console = Console(file=err, width=100, force_terminal=False, no_color=True, highlight=False)
    return TerminalFrontend(console, err_console), out, err


class LineReader:
    """Scripted stdin: yields the given lines, then raises EOFError."""

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = iter(lines)

    def __call__(self) -> str:
        try:
            return next(self._lines)
        except StopIteration:
            raise EOFError from None
