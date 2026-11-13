"""Tests for the builtin hooks plugin and the hook name contract."""

from __future__ import annotations

from typing import Any

from deeppyro.builtin.hooks import HooksPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.hooks import HookService
from deeppyro.hooks.types import AGENT_PRE_STEP, HOOK_MODES, HOOK_NAMES
from deeppyro.kernel import PluginSpec, boot


class FakePluginContext:
    """Minimal PluginContext double for plugin setup."""

    def __init__(self) -> None:
        self.provided: dict[str, Any] = {}

    def provide(self, name: str, value: Any) -> None:
        self.provided[name] = value


def test_hooks_plugin_provides_the_hook_service() -> None:
    ctx = FakePluginContext()
    plugin = HooksPlugin(ctx, None)

    assert plugin.plugin_id == "hooks"
    assert plugin.provide == "hooks"
    assert plugin.inject == []
    assert isinstance(plugin.service, HookService)

    plugin.setup(ctx, None)
    assert ctx.provided["hooks"] is plugin.service


def test_hook_modes_cover_every_hook_name() -> None:
    assert set(HOOK_MODES) == set(HOOK_NAMES)
    assert set(HOOK_MODES.values()) == {"waterfall", "serial", "emit"}
    assert HOOK_MODES[AGENT_PRE_STEP] == "waterfall"
    assert HOOK_MODES["agent/request-error"] == "waterfall"
    assert HOOK_MODES["agent/turn-stopping"] == "serial"
    assert HOOK_MODES["agent/status"] == "emit"


async def test_hooks_plugin_boots_in_kernel() -> None:
    config = Config(
        providers={
            "local": ProviderConfig(
                baseURL="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )
    spec = PluginSpec(id="hooks", name="deeppyro.builtin.hooks:HooksPlugin", after="agents")
    app = await boot(config, extra_plugins=[spec])
    try:
        assert "hooks" in app.started
        service = app.service("hooks")
        assert isinstance(service, HookService)

        calls: list[dict[str, Any]] = []

        async def handler(payload: dict[str, Any], next_step: Any) -> Any:
            calls.append(payload)
            return {"kind": "allow"}

        dispose = service.on(AGENT_PRE_STEP, handler)
        result = await service.waterfall(AGENT_PRE_STEP, {"turn": 1, "step": 1}, {"kind": "allow"})
        assert result == {"kind": "allow"}
        assert calls == [{"turn": 1, "step": 1}]
        dispose()
        assert service.has(AGENT_PRE_STEP) is False
    finally:
        await app.stop()
