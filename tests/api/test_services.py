"""Typed service resolution at the api and kernel boundary.

The cases below pin the promises of the typed shell: a Service subclass binds
its own contract to its provide name, typed lookups reach it both through the
App and through PluginContext, the lookup helpers also work on a raw runtime
context, and two containers never leak a same-named service into each other.
Plugin-facing string lookup is gone in API v3; only the host-level App.service
compatibility helper is still pinned below.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.api import (
    Plugin,
    Service,
    ServiceBag,
    ServiceNotFoundError,
    ServiceTypeError,
    bind_service_type,
    lookup_service,
    require_service,
)
from deeppyro.api.services import contract_path, service_name
from deeppyro.config import Config
from deeppyro.core.bus import EventBus
from deeppyro.core.payload import field_of
from deeppyro.hooks import HookService
from deeppyro.kernel.app import create_app
from deeppyro.kernel.manifest import PluginSpec

MODULE = __name__


class UnboundContract:
    """Not a service: proves the failure mode of the type-to-name resolution."""


class WidgetPlugin(Service):
    """Typed service registered under the legacy name widget."""

    provide = "widget"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)
        self.label = str(field_of(config, "label", "widget") or "widget")

    def observed_label(self) -> str:
        return self.label


class LookerPlugin(Service):
    """Calls a typed lookup from inside a service method."""

    provide = "looker"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)

    def seen_label(self) -> str:
        return require_service(self.ctx, WidgetPlugin).label


class ProbePlugin(Service):
    """Captures the context-level lookups made during plugin setup."""

    provide = "probe"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)
        self.context: Any = None
        self.typed: Any = None
        self.alias: Any = None

    def setup(self) -> None:
        self.context = self.ctx
        self.typed = self.ctx.require(WidgetPlugin)
        self.alias = self.ctx.get(WidgetPlugin)


class LegacyStringPlugin(Plugin):
    """Old-style plugin: it only knows the string name."""

    plugin_id = "legacy-string"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)

    def setup(self) -> None:
        self.ctx.provide("legacy-service", "legacy-value")


class LegacyContract:
    """Contract for the string-only service above, bound explicitly in a test."""


def _config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "m"},
            "providers": {
                "local": {
                    "api": "openai",
                    "baseURL": "http://127.0.0.1:8000/v1",
                    "models": [{"id": "m"}],
                }
            },
        }
    )


def _spec(plugin_id: str, class_name: str, config: Any = None) -> PluginSpec:
    return PluginSpec(id=plugin_id, name=f"{MODULE}:{class_name}", config=config)


async def _boot(*specs: PluginSpec) -> Any:
    return await create_app(_config(), plugins=tuple(specs))


# -- contract resolution -------------------------------------------------


def test_service_subclass_binds_its_own_contract() -> None:
    assert service_name(WidgetPlugin) == "widget"
    assert service_name(HookService) == "hooks"
    assert service_name(EventBus) == "bus"


def test_unbound_contract_reports_the_resolution_failure() -> None:
    with pytest.raises(ServiceTypeError, match="is not bound to a service name"):
        service_name(UnboundContract)
    assert contract_path(UnboundContract) == f"{MODULE}.UnboundContract"


def test_explicit_binding_aliases_a_legacy_name() -> None:
    bind_service_type(LegacyContract, "legacy-service")
    assert service_name(LegacyContract) == "legacy-service"


# -- app level typed lookups --------------------------------------------


async def test_app_resolves_contracts_and_string_aliases() -> None:
    app = await _boot(
        _spec("widget", "WidgetPlugin"),
        _spec("looker", "LookerPlugin"),
        _spec("probe", "ProbePlugin"),
    )
    try:
        assert app.get(WidgetPlugin).label == "widget"
        assert app.require(WidgetPlugin).label == "widget"
        assert app.service("widget").label == "widget"
        assert app.has_service("widget") is True
        assert app.get(HookService) is None
        assert app.has_service("hooks") is False
        assert app.require(LookerPlugin).seen_label() == "widget"
    finally:
        await app.stop()


async def test_app_require_reports_the_missing_contract() -> None:
    app = await _boot(_spec("widget", "WidgetPlugin"))
    try:
        with pytest.raises(ServiceNotFoundError) as caught:
            app.require(HookService)
        assert caught.value.service == "hooks"
        assert caught.value.contract is HookService
        assert contract_path(HookService) in str(caught.value)
        with pytest.raises(ServiceTypeError):
            app.require(UnboundContract)
        assert app.get(UnboundContract) is None
        with pytest.raises(LookupError, match="not registered or not active"):
            app.service("hooks")
    finally:
        await app.stop()


async def test_two_containers_keep_a_same_named_service_apart() -> None:
    first = await _boot(_spec("widget", "WidgetPlugin", {"label": "one"}))
    second = await _boot(_spec("widget", "WidgetPlugin", {"label": "two"}))
    try:
        assert first.require(WidgetPlugin).label == "one"
        assert second.require(WidgetPlugin).label == "two"
        assert first.get("widget").label == "one"
        assert second.get("widget").label == "two"
    finally:
        await first.stop()
        await second.stop()


# -- plugin context lookups ---------------------------------------------


async def test_plugin_context_aliases_and_failures() -> None:
    app = await _boot(_spec("widget", "WidgetPlugin"), _spec("probe", "ProbePlugin"))
    try:
        probe = app.require(ProbePlugin)
        context = probe.context
        assert probe.typed.label == "widget"
        assert probe.alias.label == "widget"
        assert context.get(HookService) is None
        assert context.get(UnboundContract) is None
        with pytest.raises(ServiceNotFoundError) as caught:
            context.require(HookService)
        assert caught.value.service == "hooks"
        assert "probe" in str(caught.value)
        with pytest.raises(ServiceTypeError):
            context.require(UnboundContract)
        with pytest.raises(TypeError, match="typed in API v3"):
            context.get("hooks")
        with pytest.raises(AttributeError, match="implicit attribute forwarding"):
            _ = context.service
    finally:
        await app.stop()


# -- helpers on a raw runtime context -----------------------------------


async def test_lookup_helpers_accept_a_raw_runtime_context() -> None:
    app = await _boot(_spec("widget", "WidgetPlugin"))
    try:
        assert lookup_service(app.ctx, WidgetPlugin).label == "widget"
        assert lookup_service(app.ctx, UnboundContract) is None
        assert require_service(app.ctx, "widget").label == "widget"
        with pytest.raises(ServiceNotFoundError) as caught:
            require_service(app.ctx, HookService)
        assert caught.value.service == "hooks"
    finally:
        await app.stop()


# -- string-only plugins stay reachable ---------------------------------


async def test_string_only_plugin_stays_reachable() -> None:
    bind_service_type(LegacyContract, "legacy-service")
    app = await _boot(_spec("legacy", "LegacyStringPlugin"))
    try:
        assert app.service("legacy-service") == "legacy-value"
        assert app.get("legacy-service") == "legacy-value"
        assert app.get(LegacyContract) == "legacy-value"
        assert app.require(LegacyContract) == "legacy-value"
    finally:
        await app.stop()


# -- ServiceBag ---------------------------------------------------------


def test_service_bag_resolves_typed_keys() -> None:
    widget = SimpleNamespace(label="bagged")
    hooks = HookService()
    bag = ServiceBag({"widget": widget, "hooks": hooks})
    assert bag.get(WidgetPlugin) is widget
    assert bag.get(HookService) is hooks
    assert bag.get(EventBus) is None
    assert bag.get(UnboundContract) is None
    assert bag.require(WidgetPlugin) is widget
    with pytest.raises(ServiceNotFoundError) as caught:
        bag.require(EventBus)
    assert caught.value.service == "bus"
    with pytest.raises(ServiceTypeError):
        bag.require(UnboundContract)
