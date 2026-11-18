"""Integration: the built-in hook consumers actually subscribe in a real kernel.

The in-tree consumers (hook-observability, session-title, prompt-extension and
approval-policy) register their listeners eagerly in setup() and hand the
disposer to ctx.effect(...).  effect() only records a teardown callback
(kernel/fiber.py): the callable runs when the plugin is disposed, never at
setup.  Unit tests stayed green because their fake contexts executed the
callback immediately, so a real kernel boot silently lost every subscription.
This test boots the real core bundle and drives each consumer through the real
HookService, so the production wiring cannot regress again.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.builtin.prompt_extension import PromptExtensionPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.hooks import (
    AGENT_TURN_STOPPING,
    APPROVAL_REQUEST,
    LLM_STREAM,
    SESSION_CREATED,
    SYSTEM_PROMPT_ASSEMBLE,
    HookService,
)
from deeppyro.kernel import boot

#: The plugins that subscribe to hooks; every one of them must be mounted.
CONSUMERS = (
    "hook-observability",
    "session-title",
    "prompt-extension",
    "approval-policy",
)

#: Plugins that need an agent loop (or a native session) to run; the hook
#: consumers themselves do not, and mounting these would subscribe on their
#: behalf too, hiding a broken consumer wiring.
DISABLED = ("sessions", "agents", "usage", "telemetry")


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                api_key="test-key",
                models=[ModelConfig(id="m", context_window=32768)],
            )
        },
        agent={"provider": "local", "model": "m"},
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


def _instance(app: Any, plugin_id: str) -> Any:
    record = app.mount_record(plugin_id)
    assert record is not None, plugin_id
    assert record.instance is not None, plugin_id
    return record.instance


async def test_core_bundle_registers_every_builtin_hook_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _booted(tmp_path, monkeypatch) as app:
        assert app.failures == []
        for plugin_id in CONSUMERS:
            assert app.started.get(plugin_id) == "ACTIVE", (plugin_id, app.started.get(plugin_id))

        hooks = app.require(HookService)
        observability = _instance(app, "hook-observability")
        title = _instance(app, "session-title")
        extension = _instance(app, "prompt-extension")
        approval = _instance(app, "approval-policy")

        # Eager subscriptions exist right after boot, before any dispatch.
        assert hooks.has(SYSTEM_PROMPT_ASSEMBLE) is True
        assert hooks.has(APPROVAL_REQUEST) is True
        assert hooks.has(AGENT_TURN_STOPPING) is True
        assert hooks.has(SESSION_CREATED) is True
        # The registered handlers are the plugin methods, not deferred lambdas.
        assert extension._on_assemble in hooks.listeners(SYSTEM_PROMPT_ASSEMBLE)
        assert approval._on_request in hooks.listeners(APPROVAL_REQUEST)
        assert title._on_turn_stopping in hooks.listeners(AGENT_TURN_STOPPING)

        # hook-observability counts dispatches on the same bus.
        assert set(observability.snapshot().values()) == {0}
        await hooks.emit(SESSION_CREATED, {})
        await hooks.emit(SESSION_CREATED, {})
        await hooks.emit(LLM_STREAM, {})
        assert observability.snapshot()[SESSION_CREATED] == 2
        assert observability.snapshot()[LLM_STREAM] == 1

        # prompt-extension appends the configured text to the assembled prompt.
        assert isinstance(extension, PromptExtensionPlugin)
        assert extension.text == "EXTRA"
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

    # The unbind callables came from the subscriptions saved by effect(): after
    # app.stop() the plugins must be fully unsubscribed.
    for name in (SYSTEM_PROMPT_ASSEMBLE, APPROVAL_REQUEST, AGENT_TURN_STOPPING, SESSION_CREATED):
        assert hooks.has(name) is False, name
