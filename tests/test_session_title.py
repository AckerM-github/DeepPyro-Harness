"""The session-title plugin names a session from its first exchange."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.session_title import (
    PROMPT_TEMPLATE,
    SYSTEM_PROMPT,
    SessionTitlePlugin,
    TitleOptions,
    clean_title,
    options_for,
)
from deeppyro.core.protocols import TextDelta
from deeppyro.session.metadata import (
    TITLE_EVENT,
    TITLE_LLM_REQUEST_EVENT,
    TITLE_PROVIDER_FIRST_PROMPT,
)
from tests.service_doubles import service_lookup, service_require


@dataclass
class Record:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0


class FakeSession:
    def __init__(self, records: list[Record] | None = None) -> None:
        self.records = list(records or [])

    def append(self, type: str, data: dict[str, Any], **_: Any) -> int:
        self.records.append(Record(type, data, seq=len(self.records) + 1))
        return len(self.records)


def user_record(text: str = "你好，帮我看看这个仓库的结构", seq: int = 8) -> Record:
    return Record(
        "user/message",
        {
            "id": "m1",
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "source": {"kind": "user"},
        },
        seq=seq,
    )


class FakeHooks:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.observed = False

    def on(self, name: str, handler: Any, **_: Any) -> Any:
        self.handlers.setdefault(name, []).append(handler)
        return lambda: self.handlers[name].remove(handler)

    def has(self, name: str) -> bool:
        return bool(self.handlers.get(name))


class FakeProvider:
    def __init__(self, chunks: list[str] | None = None, error: Exception | None = None) -> None:
        self.chunks = list(chunks if chunks is not None else ["  你好", "，世界。  "])
        self.error = error
        self.requests: list[Any] = []

    async def stream(self, request: Any) -> Any:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield TextDelta(chunk)


class FakeLLM:
    def __init__(self, provider: FakeProvider) -> None:
        self.instance = provider

    def provider(self, name: str) -> FakeProvider:
        return self.instance


class FakeContext:
    def __init__(self, *, hooks: Any = None, llm: Any = None, title: Any = None) -> None:
        self.hooks = hooks
        self.llm = llm
        self.title = title
        self.effects: list[str] = []

    def service(self, name: str) -> Any:
        if name == "hooks":
            return self.hooks
        if name == "llm":
            return self.llm
        if name == "config":
            return SimpleNamespace(value=SimpleNamespace(title=self.title))
        raise KeyError(name)

    def _service_map(self) -> dict[str, Any]:
        return {
            "hooks": self.hooks,
            "llm": self.llm,
            "config": SimpleNamespace(value=SimpleNamespace(title=self.title)),
        }

    def get(self, service: str | type[Any]) -> Any | None:
        return service_lookup(self._service_map(), service)

    def require(self, service: type[Any]) -> Any:
        return service_require(self._service_map(), service)

    def effect(self, callback: Any, label: str = "") -> Any:
        self.effects.append(label)
        return callback()


class FakeAgent:
    def __init__(
        self,
        session: FakeSession,
        *,
        turn: int = 1,
        model: str = "deepseek-flash",
        provider_name: str = "deepseek",
    ) -> None:
        self.session = session
        self.turn = turn
        self.model = model
        self.provider_name = provider_name


def make_plugin(
    *,
    title: Any = None,
    chunks: list[str] | None = None,
    error: Exception | None = None,
    records: list[Record] | None = None,
    turn: int = 1,
) -> tuple[SessionTitlePlugin, FakeContext, FakeSession, FakeProvider, FakeAgent, FakeHooks]:
    session = FakeSession(records if records is not None else [user_record()])
    provider = FakeProvider(chunks, error)
    hooks = FakeHooks()
    ctx = FakeContext(hooks=hooks, llm=FakeLLM(provider), title=title)
    plugin = SessionTitlePlugin(ctx, None)
    agent = FakeAgent(session, turn=turn)
    return plugin, ctx, session, provider, agent, hooks


async def fire(plugin: SessionTitlePlugin, agent: FakeAgent) -> None:
    plugin._on_turn_stopping({"agent": agent, "turn": agent.turn, "signal": None, "finalText": "hi"})
    if plugin._tasks:
        await asyncio.gather(*list(plugin._tasks))


@pytest.mark.asyncio
async def test_first_turn_records_the_request_and_the_title() -> None:
    plugin, _ctx, session, provider, agent, hooks = make_plugin()
    await plugin.setup()
    assert hooks.has("agent/turn-stopping")

    await fire(plugin, agent)

    assert [record.type for record in session.records[-2:]] == [
        TITLE_LLM_REQUEST_EVENT,
        TITLE_EVENT,
    ]
    request = session.records[-2].data
    assert request["titleProvider"] == TITLE_PROVIDER_FIRST_PROMPT
    assert request["messageSeqs"] == [8]
    assert request["route"] == {"provider": "deepseek", "model": "deepseek-flash"}
    assert request["system"] == SYSTEM_PROMPT
    assert request["maxTokens"] == 64
    assert request["reasoningEffort"] == "off"
    assert request["messages"][0]["source"] == {"kind": "dpyro-session-title-llm"}
    assert "你好，帮我看看" in request["messages"][0]["content"][0]["text"]
    assert session.records[-1].data == {
        "title": "你好，世界",
        "source": {"kind": "llm"},
    }

    sent = provider.requests[0]
    assert sent.model == "deepseek-flash"
    assert sent.max_tokens == 64
    assert sent.system == SYSTEM_PROMPT
    assert sent.messages[0].content[0].text.startswith("Generate the session title")


def test_prompt_matches_the_dsh_shape() -> None:
    assert PROMPT_TEMPLATE.format(payload="[]").startswith("Generate the session title")


@pytest.mark.asyncio
async def test_an_existing_title_is_never_overwritten() -> None:
    records = [user_record(), Record(TITLE_EVENT, {"title": "manual", "source": {"kind": "user"}})]
    plugin, _ctx, session, _provider, agent, _hooks = make_plugin(records=records)
    await plugin.setup()
    await fire(plugin, agent)
    assert len(session.records) == 2


@pytest.mark.asyncio
async def test_only_the_first_turn_is_named() -> None:
    plugin, _ctx, session, _provider, agent, _hooks = make_plugin(turn=2)
    await plugin.setup()
    await fire(plugin, agent)
    assert len(session.records) == 1


@pytest.mark.asyncio
async def test_disabled_skips_registration() -> None:
    plugin, _ctx, _session, _provider, _agent, hooks = make_plugin(title={"enabled": False})
    assert plugin.options.enabled is False
    await plugin.setup()
    assert hooks.handlers == {}


@pytest.mark.asyncio
async def test_failure_keeps_the_fallback_title() -> None:
    plugin, _ctx, session, _provider, agent, _hooks = make_plugin(error=RuntimeError("boom"))
    await plugin.setup()
    await fire(plugin, agent)
    assert [record.type for record in session.records] == [
        "user/message",
        TITLE_LLM_REQUEST_EVENT,
    ]


def test_clean_title_takes_one_line_and_drops_decoration() -> None:
    assert clean_title("「仓库结构梳理」", limit=24) == "仓库结构梳理"
    assert clean_title("Title: Refactor the gate\nmore text", limit=24) == "Refactor the gate"
    assert clean_title("**bold**", limit=24) == "bold"
    assert clean_title("标题：修复编译错误", limit=24) == "修复编译错误"
    assert clean_title("", limit=24) is None
    long_title = "一二三四五六七八九十一二三四五六七八九十"
    assert len(clean_title(long_title, limit=5) or "") <= 6


def test_options_default_when_the_table_is_broken() -> None:
    assert options_for(FakeContext(title={"maxChars": "lots"})) == TitleOptions()
    assert options_for(FakeContext(title={"maxChars": 12})).maxChars == 12
