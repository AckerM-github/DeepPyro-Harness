"""Shared fakes and helpers for the subagent tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

from deeppyro.core.protocols import ToolResult
from deeppyro.session.session import Session
from deeppyro.session.store import SessionStore
from deeppyro.subagent.manager import SubagentService
from deeppyro.tools.registry import ToolRegistry


class FakeAgent:
    """Agent double: records prompts, returns a fixed reply and can be gated."""

    def __init__(
        self,
        reply: str = "ok",
        *,
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.reply = reply
        self.gate = gate
        self.error = error
        self.prompts: list[str] = []
        self.closed = False
        self.close_calls = 0

    async def send(self, text: str) -> str:
        self.prompts.append(text)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.reply

    async def aclose(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeAgentNoClose(FakeAgent):
    """Agent double without the optional aclose hook."""

    aclose = None


class FakeAgents:
    """Agents service double that records options()/create() calls."""

    def __init__(
        self,
        *,
        reply: str = "ok",
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
        agent_factory: Any = None,
    ) -> None:
        self.reply = reply
        self.gate = gate
        self.error = error
        self.agent_factory = agent_factory
        self.options_calls: list[dict[str, Any]] = []
        self.created: list[dict[str, Any]] = []
        self.agents: list[FakeAgent] = []

    def options(self, **overrides: Any) -> Any:
        self.options_calls.append(dict(overrides))
        return SimpleNamespace(**overrides)

    def create(self, **kwargs: Any) -> FakeAgent:
        if self.agent_factory is not None:
            agent = self.agent_factory()
        else:
            agent = FakeAgent(self.reply, gate=self.gate, error=self.error)
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


class FakeSandboxNoAsk(FakeSandbox):
    """Sandbox double whose gate() does not accept the optional ask keyword."""

    def gate(self, workspace: Any, **kwargs: Any) -> FakeGate:
        if kwargs.get("ask") is not None:
            raise TypeError("gate() got an unexpected keyword argument 'ask'")
        return super().gate(workspace)


class FakeLlm:
    """LLM service double used to resolve named providers."""

    def __init__(self, providers: dict[str, Any] | None = None) -> None:
        self.providers = dict(providers or {})
        self.calls: list[str] = []

    def provider(self, name: str) -> Any:
        self.calls.append(name)
        try:
            return self.providers[name]
        except KeyError as exc:
            raise LookupError(f"unknown provider {name}") from exc


class FakeTool:
    """Minimal Tool implementation for registry tests."""

    parallel_safe = True
    description = "fake tool"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult()


def make_registry(names: tuple[str, ...] = ("read", "write", "subagent", "team_spawn", "team_wait")) -> Any:
    """Build a ToolRegistry with the given tool names."""
    return ToolRegistry([FakeTool(name) for name in names])


class FakeWithoutRegistry:
    """Registry double exposing without(); records the excluded names."""

    def __init__(self, names: tuple[str, ...] = ("read", "write", "subagent", "team_spawn")) -> None:
        self.tools = [FakeTool(name) for name in names]
        self.calls: list[tuple[str, ...]] = []

    def without(self, names: Any) -> ToolRegistry:
        requested = tuple(names)
        self.calls.append(requested)
        return ToolRegistry([tool for tool in self.tools if tool.name not in requested])


@dataclass(slots=True)
class SubagentEnv:
    """Everything a subagent test needs: configured service plus its doubles."""

    service: SubagentService
    parent: Session
    store: SessionStore
    agents: FakeAgents
    sandbox: Any
    permission: FakeGate
    provider: SimpleNamespace
    registry: Any
    llm: FakeLlm


def make_subagent(
    tmp_path: Path,
    *,
    agents: FakeAgents | None = None,
    sandbox: Any = None,
    permission: FakeGate | None = None,
    registry: Any = None,
    services: dict[str, Any] | None = None,
    preset: str = "default",
    preset_revision: int | None = 7,
    model: str = "parent-model",
    context_window: int = 8192,
    ask: Any = "ask-callback",
    max_runs: int = 4,
    configure: bool = True,
) -> SubagentEnv:
    """Build a SubagentService bound to a real temporary parent session."""
    store = SessionStore(root=tmp_path / "sessions")
    parent = store.create(tmp_path)
    agents = agents or FakeAgents()
    sandbox = sandbox if sandbox is not None else FakeSandbox()
    permission = permission or FakeGate("workspace-write", "ask")
    provider = SimpleNamespace(name="local")
    llm = FakeLlm({"local": provider, "other": SimpleNamespace(name="other")})
    registry = registry if registry is not None else make_registry()
    service = SubagentService(
        agents=agents,
        sessions=SimpleNamespace(store=store),
        sandbox=sandbox,
        max_runs=max_runs,
    )
    if configure:
        service.configure(
            cwd=tmp_path,
            provider=provider,
            registry=registry,
            permission=permission,
            model=model,
            context_window=context_window,
            preset=preset,
            preset_revision=preset_revision,
            services=dict(services or {"llm": llm}),
            ask=ask,
        )
    service.bind_session(parent)
    return SubagentEnv(
        service=service,
        parent=parent,
        store=store,
        agents=agents,
        sandbox=sandbox,
        permission=permission,
        provider=provider,
        registry=registry,
        llm=llm,
    )


def records(session: Session, record_type: str) -> list[dict[str, Any]]:
    """Return the data payloads of one record type in append order."""
    return [record.data for record in session.records if record.type == record_type]


async def spin(times: int = 3) -> None:
    """Yield to the event loop so background run tasks can start."""
    for _ in range(times):
        await asyncio.sleep(0)


async def wait_for(predicate: Any, *, attempts: int = 200) -> None:
    """Yield until predicate() is truthy."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")
