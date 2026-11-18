"""End-to-end v4 usage reporting: hooks drive UsageService from session facts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.builtin.usage import USAGE_DIR_ENV, UsageService
from deeppyro.config import Config
from deeppyro.core.protocols import TextDelta
from deeppyro.hooks import SESSION_DISPOSED, HookService
from tests.agent.fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
    usage,
)
from tests.service_doubles import service_lookup, service_require


class FakeCtx:
    """Minimal plugin context: eager effects, typed service lookup."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = services
        self.provided: dict[str, Any] = {}
        self.effects: list[str] = []

    def provide(self, name: str, value: Any = None, check: Any = None) -> Any:
        self.provided[name] = value
        return lambda: self.provided.pop(name, None)

    def get(self, service: Any) -> Any:
        return service_lookup(self._services, service)

    def require(self, service: Any) -> Any:
        return service_require(self._services, service)

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        # Production semantics: record the teardown callback; never run it now.
        self.effects.append(label)
        return execute


@pytest.fixture(autouse=True)
def usage_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep ledger writes inside the test directory."""
    target = tmp_path / "usage"
    monkeypatch.setenv(USAGE_DIR_ENV, str(target))
    return target


def make_service() -> tuple[HookService, UsageService]:
    hooks = HookService()
    ctx = FakeCtx({"hooks": hooks, "config": SimpleNamespace(value=Config())})
    service = UsageService(ctx, None)
    service.setup()
    return hooks, service


def make_agent(
    tmp_path: Path,
    hooks: HookService,
    script: list[ScriptedTurn],
) -> tuple[FakeSessionLog, AgentSession]:
    session = FakeSessionLog(cwd=tmp_path)
    agent = AgentSession(
        session=session,
        provider=ScriptedProvider(script),
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=RecordingSink(session),
        permission=FakePermission(),
        cwd=tmp_path,
        services={"hooks": hooks},
    )
    return session, agent


async def test_v4_turn_is_reported_from_session_records(tmp_path: Path) -> None:
    hooks, service = make_service()
    script = [ScriptedTurn(events=[TextDelta("hi"), usage(cached=16), finish("stop")])]
    session, agent = make_agent(tmp_path, hooks, script)

    assert await agent.send("ping") == "hi"

    records = service.records()
    assert len(records) == 1
    record = records[0]
    assert record.source == "v4"
    assert record.session_id == session.session_id
    assert record.provider == "fake-provider"
    assert record.model == "test-model"
    assert record.turn == 1
    assert (record.input_tokens, record.output_tokens, record.cached_tokens) == (10, 5, 16)
    assert record.stop_reason == "stop"
    assert record.latency_ms >= 0
    assert service.totals().requests == 1
    assert service.by_session()[session.session_id].requests == 1
    lines = service.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


async def test_two_v4_turns_are_reported_once_each(tmp_path: Path) -> None:
    hooks, service = make_service()
    script = [
        ScriptedTurn(events=[TextDelta("one"), usage(), finish("stop")]),
        ScriptedTurn(events=[TextDelta("two"), usage(), finish("stop")]),
    ]
    session, agent = make_agent(tmp_path, hooks, script)

    assert await agent.send("first") == "one"
    assert await agent.send("second") == "two"

    records = service.records()
    assert [record.turn for record in records] == [1, 2]
    assert all(record.session_id == session.session_id for record in records)
    assert service.totals().requests == 2


async def test_session_disposal_flushes_an_unreported_turn(tmp_path: Path) -> None:
    hooks, service = make_service()
    script = [ScriptedTurn(events=[TextDelta("long"), usage(), finish("length")])]
    session, agent = make_agent(tmp_path, hooks, script)

    assert await agent.send("go long") == "long"
    # A length-truncated turn does not pass the turn-stopping hook: the report
    # is flushed on session disposal instead, so no usage is ever lost.
    assert service.records() == ()
    await hooks.emit(SESSION_DISPOSED, {"sessionId": session.session_id})

    records = service.records()
    assert len(records) == 1
    assert records[0].stop_reason == "length"
