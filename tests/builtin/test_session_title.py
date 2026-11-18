"""Native session-title naming for schema 1 sessions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.session_title import (
    NATIVE_TITLE_REQUEST_EVENT,
    TITLE_PROVIDER_FIRST_PROMPT,
    SessionTitlePlugin,
    _first_native_user_message,
    _native_session_of,
    _native_title,
)
from deeppyro.core.llm import TextDelta
from deeppyro.session.native_index import list_native_sessions
from deeppyro.session.schema import EventKind, NativeCodec, NativeSession
from tests.service_doubles import service_lookup, service_require
from tests.session.native_helpers import SESSION_ID, TITLE, write_native


class FakeHooks:
    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

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
        # Production semantics: record the teardown callback; never run it now.
        self.effects.append(label)
        return callback


class FakeAgent:
    def __init__(
        self,
        session: Any,
        *,
        turn: int = 1,
        model: str = "deepseek-flash",
        provider_name: str = "deepseek",
    ) -> None:
        self.session = session
        self.turn = turn
        self.model = model
        self.provider_name = provider_name


def make_native_plugin(
    *,
    chunks: list[str] | None = None,
    error: Exception | None = None,
) -> tuple[SessionTitlePlugin, FakeProvider, FakeHooks]:
    provider = FakeProvider(chunks, error)
    hooks = FakeHooks()
    ctx = FakeContext(hooks=hooks, llm=FakeLLM(provider), title=None)
    plugin = SessionTitlePlugin(ctx, None)
    return plugin, provider, hooks


async def fire(plugin: SessionTitlePlugin, agent: FakeAgent) -> None:
    plugin._on_turn_stopping({"agent": agent, "turn": agent.turn, "signal": None, "finalText": "hi"})
    if plugin._tasks:
        await asyncio.gather(*list(plugin._tasks))


def _events(path: Any) -> tuple[Any, ...]:
    return NativeCodec.read(path).events


def test_native_session_is_detected_through_the_handle(tmp_path: Any) -> None:
    session = NativeSession.open(write_native(tmp_path))
    try:
        handle = SimpleNamespace(native_session=session)
        assert _native_session_of(session) is session
        assert _native_session_of(handle) is session
        assert _native_session_of(object()) is None
        assert _native_session_of(SimpleNamespace(native_session="nope")) is None
    finally:
        session.close()


def test_helpers_find_the_first_user_message_and_title(tmp_path: Any) -> None:
    untitled = _events(write_native(tmp_path, title=None))
    titled = _events(write_native(tmp_path / "second", session_id=SESSION_ID, title=TITLE))

    assert _first_native_user_message(untitled) == (2, "Please read the file.")
    assert _native_title(untitled) is None
    assert _native_title(titled) == TITLE
    assert _first_native_user_message(()) is None


@pytest.mark.asyncio
async def test_name_native_session_appends_request_and_title(tmp_path: Any) -> None:
    path = write_native(tmp_path, title=None)
    session = NativeSession.open(path)
    plugin, provider, _hooks = make_native_plugin(chunks=["Fix ", "the parser"])
    try:
        title = await plugin.name_native_session(session, provider_name="fake", model="fake-1")
    finally:
        session.close()

    assert title == "Fix the parser"
    events = _events(path)
    assert [event.type for event in events[-2:]] == [
        NATIVE_TITLE_REQUEST_EVENT,
        EventKind.SESSION_TITLE.value,
    ]
    request = events[-2].data
    assert request["titleProvider"] == TITLE_PROVIDER_FIRST_PROMPT
    assert request["messageSeqs"] == [2]
    assert request["route"] == {"provider": "fake", "model": "fake-1"}
    assert request["system"]
    assert events[-1].data == {"title": "Fix the parser", "source": "llm"}
    assert list_native_sessions(tmp_path)[0].title == "Fix the parser"
    assert provider.requests[0].model == "fake-1"
    assert provider.requests[0].max_tokens == 64


@pytest.mark.asyncio
async def test_native_name_never_overwrites_or_touches_an_existing_title(tmp_path: Any) -> None:
    path = write_native(tmp_path, title=TITLE)
    session = NativeSession.open(path)
    before = len(_events(path))
    plugin, provider, _hooks = make_native_plugin()
    try:
        title = await plugin.name_native_session(session, provider_name="fake", model="fake-1")
    finally:
        session.close()

    assert title is None
    assert len(_events(path)) == before
    assert provider.requests == []


@pytest.mark.asyncio
async def test_native_name_swallows_provider_failures(tmp_path: Any) -> None:
    path = write_native(tmp_path, title=None)
    session = NativeSession.open(path)
    plugin, _provider, _hooks = make_native_plugin(error=RuntimeError("boom"))
    try:
        title = await plugin.name_native_session(session, provider_name="fake", model="fake-1")
    finally:
        session.close()

    assert title is None
    assert [event.type for event in _events(path)][-1] == NATIVE_TITLE_REQUEST_EVENT


@pytest.mark.asyncio
async def test_hook_names_an_open_native_session(tmp_path: Any) -> None:
    path = write_native(tmp_path, title=None)
    session = NativeSession.open(path)
    plugin, _provider, hooks = make_native_plugin(chunks=["Fork the parser"])
    await plugin.setup()
    assert hooks.has("agent/turn-stopping")
    agent = FakeAgent(session, turn=1)
    try:
        await fire(plugin, agent)
    finally:
        session.close()

    events = _events(path)
    assert [event.type for event in events[-2:]] == [
        NATIVE_TITLE_REQUEST_EVENT,
        EventKind.SESSION_TITLE.value,
    ]
    assert list_native_sessions(tmp_path)[0].title == "Fork the parser"


@pytest.mark.asyncio
async def test_native_hook_respects_turn_and_title_guards(tmp_path: Any) -> None:
    titled = write_native(tmp_path / "a", title=TITLE)
    plugin, _provider, _hooks = make_native_plugin()
    session = NativeSession.open(titled)
    try:
        await fire(plugin, FakeAgent(session, turn=2))
        await fire(plugin, FakeAgent(session, turn=1))
    finally:
        session.close()
    assert len(_events(titled)) == 9
    assert plugin._tasks == set()
