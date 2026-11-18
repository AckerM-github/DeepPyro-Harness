"""Integration: the built-in hook consumers actually subscribe in a real kernel.

Six built-in plugins used to register their listeners inside ctx.effect(...).
effect() only records a teardown callback (kernel/fiber.py): the callable runs
when the plugin is disposed, never at setup.  Unit tests stayed green because
their fake contexts executed the callback immediately, so a real kernel boot
silently lost every subscription.  These tests boot the real core bundle and
drive each consumer through the real HookService, so the production wiring
cannot regress again.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.builtin.prompt_extension import PromptExtensionPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.hooks import (
    AGENT_REQUEST_ERROR,
    AGENT_STATUS,
    AGENT_TURN_STOPPING,
    APPROVAL_REQUEST,
    LLM_STREAM,
    SESSION_CREATED,
    SYSTEM_PROMPT_ASSEMBLE,
    HookService,
)
from deeppyro.hooks.records import INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE
from deeppyro.kernel import boot
from deeppyro.session import SessionStore
from tests.agent.fakes import FakePermission, FakeRegistry, RecordingSink, ScriptedProvider

#: The plugins that subscribe to hooks; every one of them must be mounted.
CONSUMERS = (
    "llm-retry",
    "hook-observability",
    "hook-journal",
    "session-title",
    "prompt-extension",
    "approval-policy",
)

#: Plugins that need an agent loop to run; the hook consumers do not.
DISABLED = ("sessions", "agents", "usage", "telemetry")


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                apiKey="test-key",
                models=[ModelConfig(id="m", contextWindow=32768)],
            )
        },
        agent={"provider": "local", "model": "m", "permissionPreset": "workspace-write"},
        sessionDir=tmp_path / "sessions",
        approval={"hookMode": "deny", "hookTools": ["write"]},
        prompt={"extension": "EXTRA"},
    )


def _manifest(tmp_path: Path) -> str:
    rows = "".join(f'[[plugins]]\nid = "{plugin_id}"\ndisabled = true\n' for plugin_id in DISABLED)
    path = tmp_path / "plugins.toml"
    path.write_text(rows, encoding="utf-8")
    return str(path)


@contextlib.asynccontextmanager
async def _booted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Boot the packaged core bundle into a hermetic home and stop it afterwards."""
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    app = await boot(_config(tmp_path), manifest_paths=[_manifest(tmp_path)])
    try:
        yield app
    finally:
        await app.stop()


def _real_session_agent(tmp_path: Path) -> tuple[SessionStore, Any, AgentSession]:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path, session_id="builtin-hooks-real")
    agent = AgentSession(
        session=session,
        provider=ScriptedProvider([]),
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=RecordingSink(session),
        permission=FakePermission(),
        cwd=tmp_path,
    )
    return store, session, agent


async def test_core_bundle_registers_every_builtin_hook_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _booted(tmp_path, monkeypatch) as app:
        assert app.failures == []
        for plugin_id in CONSUMERS:
            assert app.started.get(plugin_id) == "ACTIVE", (plugin_id, app.started.get(plugin_id))

        retry = app.mount_record("llm-retry").instance
        hooks: HookService = retry._hooks
        observability = app.mount_record("hook-observability").instance
        title = app.mount_record("session-title").instance
        extension = app.mount_record("prompt-extension").instance
        approval = app.mount_record("approval-policy").instance

        # Eager subscriptions exist right after boot, before any dispatch.
        assert hooks.has(SYSTEM_PROMPT_ASSEMBLE) is True
        assert hooks.has(APPROVAL_REQUEST) is True
        assert hooks.has(AGENT_REQUEST_ERROR) is True
        assert hooks.has(AGENT_TURN_STOPPING) is True
        assert hooks.observed is True
        # The registered handlers are the plugin methods, not deferred lambdas.
        assert extension._on_assemble in hooks.listeners(SYSTEM_PROMPT_ASSEMBLE)
        assert approval._on_request in hooks.listeners(APPROVAL_REQUEST)
        assert retry._on_request_error in hooks.listeners(AGENT_REQUEST_ERROR)
        assert title._on_turn_stopping in hooks.listeners(AGENT_TURN_STOPPING)

        # hook-observability counts dispatches on the same bus.
        assert set(observability.snapshot().values()) == {0}
        await hooks.emit(SESSION_CREATED, {})
        await hooks.emit(SESSION_CREATED, {})
        await hooks.emit(LLM_STREAM, {})
        assert observability.snapshot()[SESSION_CREATED] == 2
        assert observability.snapshot()[LLM_STREAM] == 1

        # prompt-extension appends the configured text to the assembled prompt.
        rendered = await hooks.waterfall(SYSTEM_PROMPT_ASSEMBLE, {"sections": ()}, "BASE")
        assert rendered == "BASE\n\nEXTRA"
        assert observability.snapshot()[SYSTEM_PROMPT_ASSEMBLE] == 1

        # approval-policy answers from [approval] and only for [approval].hookTools.
        denied = await hooks.waterfall(APPROVAL_REQUEST, {"request": SimpleNamespace(tool="write")}, "ask")
        passed = await hooks.waterfall(APPROVAL_REQUEST, {"request": SimpleNamespace(tool="read")}, "ask")
        assert (denied, passed) == ("deny", "ask")
        assert approval.mode == "deny"
        assert approval.tools == ("write",)
        assert observability.snapshot()[APPROVAL_REQUEST] == 2

        # llm-retry answers agent/request-error when the session supplied a policy.
        fallback = {"kind": "none"}
        retried = await hooks.waterfall(AGENT_REQUEST_ERROR, {"retryPolicy": object()}, fallback)
        assert retried == {"kind": "retry"}
        assert await hooks.waterfall(AGENT_REQUEST_ERROR, {}, fallback) == fallback

    # The unbind callables came from the subscriptions saved by effect(): after
    # app.stop() the plugins must be fully unsubscribed.
    assert hooks.has(SYSTEM_PROMPT_ASSEMBLE) is False
    assert hooks.has(APPROVAL_REQUEST) is False
    assert hooks.has(AGENT_REQUEST_ERROR) is False
    assert hooks.has(AGENT_TURN_STOPPING) is False
    assert hooks.observed is False


async def test_hook_journal_persists_and_session_title_schedules_in_a_real_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _booted(tmp_path, monkeypatch) as app:
        hooks: HookService = app.mount_record("llm-retry").instance._hooks
        journal = app.mount_record("hook-journal").instance
        title = app.mount_record("session-title").instance
        assert title.options.enabled is True
        records_before = journal.records_written

        # hook-journal writes the invoked/result pair into the real agent session.
        store, session, agent = _real_session_agent(tmp_path)
        await hooks.emit(AGENT_STATUS, {"agent": agent, "status": "thinking"})
        assert journal.records_written == records_before + 2
        assert [record.type for record in session.records] == [INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE]
        session.close()
        reopened = store.open("builtin-hooks-real")
        try:
            assert [record.type for record in reopened.records] == [INVOKED_RECORD_TYPE, RESULT_RECORD_TYPE]
        finally:
            reopened.close()

        # session-title schedules its naming request at the end of the first turn.
        scheduled = asyncio.Event()
        release = asyncio.Event()

        async def blocked_name(_agent: Any, _session: Any) -> None:
            scheduled.set()
            await release.wait()

        monkeypatch.setattr(title, "_name", blocked_name)
        duck_agent = SimpleNamespace(turn=1, session=SimpleNamespace(records=[], append=lambda *a, **k: None))
        try:
            results = await hooks.serial(AGENT_TURN_STOPPING, {"agent": duck_agent})
            assert results == [None]
            await asyncio.wait_for(scheduled.wait(), 1.0)
            assert len(title._tasks) == 1
        finally:
            release.set()
            pending = list(title._tasks)
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), 1.0)
            # the done-callback discards the task on a later loop iteration
            for _ in range(10):
                if not title._tasks:
                    break
                await asyncio.sleep(0)
        assert title._tasks == set()

        # The extension text is what the real plugin read from [prompt].
        extension = app.mount_record("prompt-extension").instance
        assert isinstance(extension, PromptExtensionPlugin)
        assert extension.text == "EXTRA"
