"""Shared fakes and helpers for the team service tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.session.session import Session
from deeppyro.session.store import SessionStore
from deeppyro.team.manager import TeamService


class FakeAgent:
    """Agent double: records prompts and returns a fixed reply, optionally gated."""

    def __init__(self, reply: str = "ok", *, gate: asyncio.Event | None = None) -> None:
        self.reply = reply
        self.gate = gate
        self.prompts: list[str] = []
        self.interrupted = False

    async def send(self, text: str) -> str:
        self.prompts.append(text)
        if self.gate is not None:
            await self.gate.wait()
        return self.reply

    async def interrupt(self) -> None:
        self.interrupted = True


class FakeAgents:
    """Agents service double that records options()/create() calls."""

    def __init__(self, *, reply: str = "ok", gate: asyncio.Event | None = None) -> None:
        self.reply = reply
        self.gate = gate
        self.options_calls: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.agents: list[FakeAgent] = []

    def options(self, **overrides: Any) -> Any:
        self.options_calls.append(dict(overrides))
        return SimpleNamespace(**overrides)

    def create(self, **kwargs: Any) -> FakeAgent:
        agent = FakeAgent(self.reply, gate=self.gate)
        self.created.append(kwargs)
        self.agents.append(agent)
        return agent


class FakeGate:
    """Permission gate double exposing the mutable preset/approval pair."""

    def __init__(self, preset: str | None = None, approval: str | None = None) -> None:
        self.preset = preset
        self.approval = approval

    def set_preset(self, preset: str) -> None:
        self.preset = preset

    def set_approval(self, approval: str) -> None:
        self.approval = approval


class FakeSandbox:
    """Sandbox service double: one fake gate per call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.gates: list[FakeGate] = []

    def gate(self, workspace: Any, ask: Any = None) -> FakeGate:
        gate = FakeGate()
        self.calls.append({"workspace": Path(workspace), "ask": ask})
        self.gates.append(gate)
        return gate


@dataclass(slots=True)
class TeamEnv:
    """Everything a team test needs: configured service plus its doubles."""

    service: TeamService
    parent: Session
    store: SessionStore
    agents: FakeAgents
    sandbox: FakeSandbox
    permission: FakeGate
    provider: SimpleNamespace


def make_team(
    tmp_path: Path,
    *,
    agents: FakeAgents | None = None,
    sandbox: FakeSandbox | None = None,
    permission: FakeGate | None = None,
    services: dict[str, Any] | None = None,
    preset: str = "default",
    model: str = "parent-model",
    registry: Any = "registry",
    context_window: int = 8192,
    ask: Any = "ask-callback",
) -> TeamEnv:
    """Build a TeamService bound to a real temporary parent session."""
    store = SessionStore(root=tmp_path / "sessions")
    parent = store.create(tmp_path)
    agents = agents or FakeAgents()
    sandbox = sandbox or FakeSandbox()
    permission = permission or FakeGate("danger-full-access", "always")
    provider = SimpleNamespace(name="local")
    service = TeamService(
        agents=agents,
        sessions=SimpleNamespace(store=store),
        sandbox=sandbox,
    )
    service.configure(
        cwd=tmp_path,
        provider=provider,
        registry=registry,
        permission=permission,
        model=model,
        context_window=context_window,
        preset=preset,
        preset_revision=None,
        services=dict(services or {}),
        ask=ask,
    )
    service.bind_session(parent)
    return TeamEnv(
        service=service,
        parent=parent,
        store=store,
        agents=agents,
        sandbox=sandbox,
        permission=permission,
        provider=provider,
    )


def records(session: Session, record_type: str) -> list[dict[str, Any]]:
    """Return the data payloads of one record type in append order."""
    return [record.data for record in session.records if record.type == record_type]


async def spin(times: int = 3) -> None:
    """Yield to the event loop so freshly created member tasks can start."""
    for _ in range(times):
        await asyncio.sleep(0)
