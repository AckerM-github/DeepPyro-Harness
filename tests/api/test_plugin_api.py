"""Plugin API v3: a single mount shape, setup(self), typed injections.

API v3 constructs a plugin with (ctx, config) and calls setup(self). The v1/v2
signatures -- setup(ctx, config) and api_version < 3 -- are rejected with a
migration hint instead of a compatibility branch (ADR 0001 section 7). These
cases pin the hard cut plus the contract-to-name resolution of inject.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from deeppyro.api import API_VERSION, Plugin, PluginError, inject_names, invoke_setup
from deeppyro.builtin.commands import CommandsPlugin


class Ctx:
    """Stand-in for PluginContext; setup only records what it is given."""


def make_plugin(cls: type[Plugin]) -> Plugin:
    return cls(Ctx(), {"name": "demo"})


class V3Plugin(Plugin):
    plugin_id = "v3"

    def setup(self) -> None:
        self.seen = ("v3", self.config["name"])


class LegacyPlugin(Plugin):
    plugin_id = "legacy"

    def setup(self, ctx: object, config: object) -> None:
        self.seen = (ctx, config)


class DeclaredV2Plugin(Plugin):
    api_version = 2

    plugin_id = "declared-v2"

    def setup(self) -> None:
        self.seen = True


class AsyncV3Plugin(Plugin):
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


def test_api_version_is_three() -> None:
    assert API_VERSION == 3
    assert Plugin.api_version == API_VERSION


def test_setup_runs_without_arguments() -> None:
    plugin = make_plugin(V3Plugin)
    assert invoke_setup(plugin, plugin.ctx, plugin.config) is None
    assert plugin.seen == ("v3", "demo")


def test_positional_setup_is_rejected_with_a_migration_hint() -> None:
    plugin = make_plugin(LegacyPlugin)
    with pytest.raises(PluginError, match="positional parameters"):
        invoke_setup(plugin, plugin.ctx, plugin.config)
    with pytest.raises(PluginError, match="no compatibility shim"):
        invoke_setup(make_plugin(LegacyPlugin), plugin.ctx, plugin.config)


def test_declared_v2_plugin_is_rejected_with_a_migration_hint() -> None:
    plugin = make_plugin(DeclaredV2Plugin)
    with pytest.raises(PluginError, match="api_version 2"):
        invoke_setup(plugin, plugin.ctx, plugin.config)


def test_async_setup_result_is_awaitable() -> None:
    plugin = make_plugin(AsyncV3Plugin)
    result = invoke_setup(plugin, plugin.ctx, plugin.config)
    assert inspect.isawaitable(result)
    assert asyncio.run(result) == "ready"


def test_base_setup_is_a_noop() -> None:
    plugin = Plugin(Ctx(), {})
    assert invoke_setup(plugin, plugin.ctx, plugin.config) is None


def test_inject_accepts_contracts_and_names() -> None:
    assert inject_names(InjectingPlugin) == ["commands", "tools"]


def test_inject_rejects_unbound_contracts() -> None:
    with pytest.raises(PluginError, match="not bound to a service name"):
        inject_names(UnboundInjectPlugin)


def test_inject_rejects_values_that_are_neither() -> None:
    with pytest.raises(PluginError, match="contract classes"):
        inject_names(GarbageInjectPlugin)
