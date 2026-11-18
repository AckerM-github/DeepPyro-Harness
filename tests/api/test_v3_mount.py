"""API v3 mount contract: one mount shape, typed injection, hard cut from v2.

These tests pin the M3 external interface: plugins are mounted through
RuntimeContext.mount only, a plugin class implements setup(self) and reads
self.ctx/self.config, services are looked up by contract class, and a plugin
that still declares the v1/v2 shape fails with a migration hint.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar

import pytest

from deeppyro.api import (
    API_VERSION,
    FiberState,
    Plugin,
    PluginContext,
    Service,
    inject_names,
)
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot
from deeppyro.kernel.app import KernelBootError, create_app
from deeppyro.kernel.composition import core_bundle_path, load_bundle, resolve_row
from deeppyro.kernel.context import RuntimeContext
from deeppyro.kernel.manifest import PluginSpec


class _DemoProvider(Service):
    provide = "v3-demo-provider"


class _InjectedPlugin(Plugin):
    plugin_id = "v3-injected"
    inject: ClassVar[tuple[type[Any], ...]] = (_DemoProvider,)

    def setup(self) -> None:
        self.provider = self.ctx.require(_DemoProvider)
        self.injected_names = inject_names(type(self))


class _PositionalSetupPlugin(Plugin):
    plugin_id = "v3-positional"

    def setup(self, ctx: Any, config: Any) -> None:  # type: ignore[override]
        raise AssertionError("API v3 must reject the positional setup signature")


class _DeclaredV2Plugin(Plugin):
    plugin_id = "v3-declared-v2"
    api_version = 2

    def setup(self) -> None:
        raise AssertionError("API v3 must reject api_version 2")


class _V3SimplePlugin(Plugin):
    plugin_id = "v3-simple"

    def setup(self) -> None:
        self.ready = True


def _config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


def _core_plugin_classes() -> list[type[Plugin]]:
    classes: list[type[Plugin]] = []
    for row in load_bundle(core_bundle_path()).rows:
        if row.group:
            continue
        target = resolve_row(row)
        if isinstance(target, type) and issubclass(target, Plugin):
            classes.append(target)
    return classes


def test_api_version_is_three_and_runtime_context_has_no_legacy_mount() -> None:
    assert API_VERSION == 3
    assert Plugin.api_version == API_VERSION
    assert hasattr(RuntimeContext, "mount")
    assert not hasattr(RuntimeContext, "plugin")
    assert not hasattr(PluginContext, "service")


async def test_default_composition_has_exactly_one_builtin_mount_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    app = await boot(_config())
    try:
        classes = _core_plugin_classes()
        assert len(classes) >= 30
        for cls in classes:
            assert cls.api_version == API_VERSION, cls.__name__
            setup = getattr(cls, "setup", None)
            assert callable(setup), cls.__name__
            assert list(inspect.signature(setup).parameters) == ["self"], cls.__name__
        assert app.started["kernel"] == "ACTIVE"
        assert app.started["tools"] == "ACTIVE"
        assert app.started["presets"] == "ACTIVE"
    finally:
        await app.stop()


async def test_runtime_context_mounts_a_plugin_with_the_host_dict_shape() -> None:
    ctx = RuntimeContext.create_root()
    seen: list[tuple[Any, Any]] = []
    fiber = ctx.mount({"name": "recorder", "apply": lambda context, config: seen.append((context, config))})
    await fiber
    assert fiber.state is FiberState.ACTIVE
    assert len(seen) == 1
    assert isinstance(seen[0][0], RuntimeContext)


async def test_plugin_context_forwards_mount_and_is_typed_only() -> None:
    raw = RuntimeContext.create_root()
    ctx = PluginContext(raw)
    fiber = ctx.mount({"name": "recorder", "apply": lambda context, config: None})
    await fiber
    assert fiber.state is FiberState.ACTIVE

    with pytest.raises(AttributeError, match="implicit attribute forwarding"):
        _ = ctx.not_a_service
    with pytest.raises(TypeError, match="typed in API v3"):
        ctx.get("tools")
    with pytest.raises(TypeError, match="typed in API v3"):
        ctx.has("tools")


async def test_inject_accepts_service_contract_types() -> None:
    app = await create_app(
        _config(),
        plugins=(
            PluginSpec(id="provider", name="tests.api.test_v3_mount:_DemoProvider"),
            PluginSpec(id="consumer", name="tests.api.test_v3_mount:_InjectedPlugin"),
        ),
    )
    try:
        assert app.started["provider"] == "ACTIVE"
        assert app.started["consumer"] == "ACTIVE"
        consumer = app.mount_record("consumer").instance
        assert isinstance(consumer.provider, _DemoProvider)
        assert consumer.injected_names == ["v3-demo-provider"]
        assert app.require(_DemoProvider) is consumer.provider
        assert consumer.ctx.require(_DemoProvider) is consumer.provider
    finally:
        await app.stop()


async def test_positional_setup_is_rejected_with_a_migration_hint() -> None:
    with pytest.raises(KernelBootError) as error:
        await create_app(
            _config(),
            plugins=(PluginSpec(id="legacy", name="tests.api.test_v3_mount:_PositionalSetupPlugin"),),
        )
    message = str(error.value)
    assert "positional parameters" in message
    assert "setup(self)" in message
    assert "no compatibility shim" in message


async def test_declared_v2_plugin_is_rejected_with_a_migration_hint() -> None:
    with pytest.raises(KernelBootError) as error:
        await create_app(
            _config(),
            plugins=(PluginSpec(id="v2", name="tests.api.test_v3_mount:_DeclaredV2Plugin"),),
        )
    message = str(error.value)
    assert "api_version 2" in message
    assert "no compatibility shim" in message


async def test_v3_plugin_mounts_through_create_app() -> None:
    app = await create_app(
        _config(),
        plugins=(PluginSpec(id="simple", name="tests.api.test_v3_mount:_V3SimplePlugin"),),
    )
    try:
        record = app.mount_record("simple")
        assert record is not None
        assert record.instance is not None
        assert record.instance.ready is True
        assert app.started["simple"] == "ACTIVE"
    finally:
        await app.stop()


async def test_builtin_presets_boot_their_realm_through_the_v3_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    app = await boot(_config())
    try:
        presets = app.require(PresetsPlugin)
        spec = presets.default()
        await presets.ensure_all()
        assert presets.is_materialized(spec.id, spec.revision)
        assert presets.realm_failures(spec.id, spec.revision) == []
    finally:
        await app.stop()
