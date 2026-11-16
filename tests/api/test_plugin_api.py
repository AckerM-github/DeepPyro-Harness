"""Plugin API v2: one injection point plus typed injections.

API v2 constructs a plugin with (ctx, config) and calls setup() with no
arguments; API v1 plugins keep receiving setup(ctx, config). These cases pin the
dispatch, the once-only deprecation warning and the contract-to-name resolution
of the inject declaration.
"""

from __future__ import annotations

import asyncio
import inspect
import warnings
from typing import Any

import pytest

from deeppyro.api import Plugin, PluginError, inject_names, invoke_setup
from deeppyro.api.plugin import _WARNED_LEGACY_SETUP
from deeppyro.builtin.commands import CommandsPlugin


class Ctx:
    """Stand-in for PluginContext; setup only records what it is given."""


def make_plugin(cls: type[Plugin]) -> Plugin:
    return cls(Ctx(), {"name": "demo"})


class V2Plugin(Plugin):
    plugin_id = "v2"

    def setup(self) -> None:
        self.seen = ("v2", self.config["name"])


class LegacyPlugin(Plugin):
    plugin_id = "legacy"

    def setup(self, ctx: Any, config: Any) -> None:
        self.seen = (ctx, config, config["name"])


class DeclaredV2LegacyPlugin(Plugin):
    api_version = 2
    plugin_id = "declared-v2-legacy"

    def setup(self, ctx: Any, config: Any) -> None:
        self.seen = (ctx, config)


class AsyncV2Plugin(Plugin):
    plugin_id = "async"

    async def setup(self) -> str:
        return "ready"


class InjectingPlugin(Plugin):
    inject = (CommandsPlugin, "tools", "")


class UnboundPlugin(Plugin):
    """A plugin class that never declared a provide name."""


class UnboundInjectPlugin(Plugin):
    inject = (UnboundPlugin,)


class GarbageInjectPlugin(Plugin):
    inject = (42,)


def test_v2_setup_runs_without_arguments() -> None:
    plugin = make_plugin(V2Plugin)
    assert invoke_setup(plugin, plugin.ctx, plugin.config) is None
    assert plugin.seen == ("v2", "demo")


def test_legacy_setup_still_receives_ctx_and_config() -> None:
    plugin = make_plugin(LegacyPlugin)
    invoke_setup(plugin, plugin.ctx, plugin.config)
    ctx, config, name = plugin.seen
    assert ctx is plugin.ctx
    assert config is plugin.config
    assert name == "demo"


def test_async_setup_result_is_awaitable() -> None:
    plugin = make_plugin(AsyncV2Plugin)
    result = invoke_setup(plugin, plugin.ctx, plugin.config)
    assert inspect.isawaitable(result)
    assert asyncio.run(result) == "ready"


def test_base_setup_is_a_noop_in_both_styles() -> None:
    plugin = Plugin(Ctx(), {})
    assert invoke_setup(plugin, plugin.ctx, plugin.config) is None


def test_declared_v2_plugin_with_legacy_signature_warns_once() -> None:
    _WARNED_LEGACY_SETUP.discard(DeclaredV2LegacyPlugin)
    plugin = make_plugin(DeclaredV2LegacyPlugin)
    with pytest.warns(DeprecationWarning, match="legacy setup"):
        invoke_setup(plugin, plugin.ctx, plugin.config)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        invoke_setup(plugin, plugin.ctx, plugin.config)


def test_inject_accepts_contracts_and_names() -> None:
    assert inject_names(InjectingPlugin) == ["commands", "tools"]


def test_inject_rejects_unbound_contracts() -> None:
    with pytest.raises(PluginError, match="not bound to a service name"):
        inject_names(UnboundInjectPlugin)


def test_inject_rejects_values_that_are_neither() -> None:
    with pytest.raises(PluginError, match="contract classes"):
        inject_names(GarbageInjectPlugin)
