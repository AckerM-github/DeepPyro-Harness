"""The inbox notifications reach the bus, not only the UI sink."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.agent.inbox import CLAIMED_EVENT, INSERTED_EVENT
from deeppyro.core.protocols import TextDelta
from deeppyro.hooks import HookService
from deeppyro.hooks.types import AGENT_STATUS

from .fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)


def _agent(tmp_path: Path, hooks: HookService) -> AgentSession:
    return AgentSession(
        session=FakeSessionLog(cwd=tmp_path),
        provider=ScriptedProvider(
            [
                ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
                ScriptedTurn(events=[TextDelta("again"), finish("stop")]),
            ]
        ),
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=RecordingSink(FakeSessionLog(cwd=tmp_path)),
        permission=FakePermission(),
        cwd=tmp_path,
        services={"hooks": hooks},
    )


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)


async def test_claimed_and_inserted_reach_the_bus(tmp_path: Path) -> None:
    hooks = HookService()
    claimed: list[Any] = []
    inserted: list[Any] = []
    hooks.on(CLAIMED_EVENT, claimed.append)
    hooks.on(INSERTED_EVENT, inserted.append)
    agent = _agent(tmp_path, hooks)

    agent.followup("next turn please")
    agent.inject("a hint for the next step")
    await agent.send("hello")
    await _settle()

    assert claimed, "the bus never saw agent/inbox/claimed"
    assert claimed[0]["message"]["id"]
    assert claimed[0]["turn"] >= 1
    texts = [payload["message"]["content"][0]["text"] for payload in inserted]
    assert "a hint for the next step" in texts
    assert all(set(payload) == {"message"} for payload in inserted)


async def test_agent_status_carries_the_agent(tmp_path: Path) -> None:
    hooks = HookService()
    seen: list[Any] = []
    hooks.on(AGENT_STATUS, seen.append)
    agent = _agent(tmp_path, hooks)

    await agent.send("hello")
    await _settle()

    assert [payload["status"] for payload in seen] == ["running", "idle"]
    assert all(payload["agent"] is agent for payload in seen)
