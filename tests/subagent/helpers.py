"""Shared native fakes and builders for the subagent tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.api.types import DEFAULT_PRESET_ID
from deeppyro.session.native_control import NativeSessionControl, NewSession
from deeppyro.session.schema import NativeSession
from deeppyro.subagent.manager import SubagentService


class FakeProvider:
    """LLM provider double: the name is all the subagent service reads."""

    def __init__(self, name: str = "local") -> None:
        self.name = name


class FakeGate:
    """Permission gate double mirroring the prompt-facts surface."""

    def __init__(self, mode: str = "workspace-write", approval: str = "ask") -> None:
        self.policy = SimpleNamespace(mode=mode)
        self.approval = approval


class FakeLlm:
    """LLM plugin double exposing the provider() factory."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def provider(self, name: str) -> Any:
        self.calls.append(name)
        if name != "local":
            raise LookupError(f"unknown provider {name}")
        return FakeProvider(name)


class FakeSessions:
    """The native control plane a sessions plugin exposes."""

    def __init__(self, root: Path) -> None:
        self.native = NativeSessionControl(root=root)
        self.created: list[NewSession] = []
        self.opened: list[str] = []

    def native_create(self, spec: NewSession) -> NativeSession:
        self.created.append(spec)
        return self.native.create(spec)

    def native_open(self, session_id: str) -> NativeSession:
        self.opened.append(session_id)
        return self.native.open(session_id)


class FakeHost:
    """A child host: sends one prompt and reports provider usage counters."""

    def __init__(
        self,
        *,
        reply: str = "child reply",
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
        usage_map: dict[str, int] | None = None,
        steps: int = 2,
    ) -> None:
        self.reply = reply
        self.error = error
        self.gate = gate
        self.usage_map = dict(usage_map or {"input": 10, "output": 5, "cached": 2})
        self.steps = steps
        self.prompts: list[str] = []
        self.closed = False
        self.restored = 0

    async def send(self, text: str) -> Any:
        self.prompts.append(text)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(final_text=self.reply, steps=self.steps)

    def usage(self) -> dict[str, int]:
        return dict(self.usage_map)

    async def aclose(self) -> None:
        self.closed = True

    def restore(self) -> bool:
        self.restored += 1
        return True


class FakeAgents:
    """The agents plugin host() factory: records every AgentBuild."""

    def __init__(
        self,
        *,
        hosts: list[FakeHost] | None = None,
        reply: str = "child reply",
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
        usage_map: dict[str, int] | None = None,
        steps: int = 2,
    ) -> None:
        self.hosts = list(hosts or [])
        self.reply = reply
        self.error = error
        self.gate = gate
        self.usage_map = usage_map
        self.steps = steps
        self.builds: list[Any] = []
        self.sessions: list[Any] = []
        self.host_objects: list[FakeHost] = []

    def host(self, session: Any, build: Any) -> FakeHost:
        self.builds.append(build)
        self.sessions.append(session)
        if self.error is not None:
            raise self.error
        if self.hosts:
            host = self.hosts.pop(0)
        else:
            host = FakeHost(
                reply=self.reply,
                gate=self.gate,
                usage_map=self.usage_map,
                steps=self.steps,
            )
        self.host_objects.append(host)
        return host


class FakeParentHost:
    """The parent native host: owns the parent session and its notice queue."""

    def __init__(self, session: NativeSession, *, busy: bool = False) -> None:
        self.native_session = session
        self.busy = busy
        self.notices: list[tuple[str, Any]] = []
        self.deliveries = 0

    def deliver(self, text: str, *, source: Any = None) -> bool:
        self.deliveries += 1
        if self.busy:
            return True
        self.notices.append((text, source))
        return False


class RecordingUsageSink:
    """Usage sink double recording every published measurement."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> None:
        self.records.append(
            {
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
            }
        )


@dataclass(slots=True)
class SubagentEnv:
    """One configured service bound to a native parent session."""

    service: SubagentService
    sessions: FakeSessions
    agents: FakeAgents
    host: FakeParentHost
    permission: FakeGate
    provider: FakeProvider
    llm: FakeLlm

    @property
    def parent(self) -> NativeSession:
        """The bound parent native session."""
        return self.host.native_session

    def open(self, session_id: str) -> NativeSession:
        """Open a native session by id through the fake sessions service."""
        return self.sessions.native.open(session_id)


def make_subagent(
    tmp_path: Path,
    *,
    agents: FakeAgents | None = None,
    reply: str = "child reply",
    error: Exception | None = None,
    usage_map: dict[str, int] | None = None,
    steps: int = 2,
    max_runs: int = 4,
    services: dict[str, Any] | None = None,
    sandbox: Any = None,
    bind: bool = True,
) -> SubagentEnv:
    """Build a native subagent service with a bound parent session."""
    sessions = FakeSessions(tmp_path)
    fake_agents = (
        agents
        if agents is not None
        else FakeAgents(reply=reply, error=error, usage_map=usage_map, steps=steps)
    )
    llm = FakeLlm()
    provider = FakeProvider()
    permission = FakeGate()
    service = SubagentService(agents=fake_agents, sessions=sessions, sandbox=sandbox, max_runs=max_runs)
    service.configure(
        cwd=tmp_path,
        provider=provider,
        registry=None,
        permission=permission,
        model="parent-model",
        context_window=4096,
        preset=DEFAULT_PRESET_ID,
        services={"llm": llm, "subagent": object()} if services is None else services,
    )
    host = FakeParentHost(sessions.native_create(NewSession(cwd=tmp_path)))
    if bind:
        service.bind_session(host)
        service.bind_parent(host)
    return SubagentEnv(
        service=service,
        sessions=sessions,
        agents=fake_agents,
        host=host,
        permission=permission,
        provider=provider,
        llm=llm,
    )


def facts(session: NativeSession, event_type: str) -> list[dict[str, Any]]:
    """Return the data payloads of the events with `event_type`."""
    payloads: list[dict[str, Any]] = []
    for event in session.events:
        if getattr(event, "type", "") == event_type:
            data = getattr(event, "data", None)
            if isinstance(data, dict):
                payloads.append(dict(data))
    return payloads


def types(session: NativeSession) -> list[str]:
    """Return the event types recorded on a native session."""
    return [getattr(event, "type", "") for event in session.events]
