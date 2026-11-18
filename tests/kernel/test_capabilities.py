"""Tests for kernel capability validation (KI-019) and the live-config whitelist."""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from pydantic import BaseModel

from deeppyro.api import Plugin, Service
from deeppyro.api.capabilities import CapabilityManifest, ServiceRequirement
from deeppyro.builtin.settings import SettingsPlugin
from deeppyro.config import Config
from deeppyro.kernel.app import KernelBootError, create_app
from deeppyro.kernel.capabilities import (
    ENTRY_ENABLED,
    ENTRY_RUNTIME,
    CapabilityIssueKind,
    CapabilityRegistry,
)
from deeppyro.kernel.composition import Composition, PluginRow
from deeppyro.kernel.runtime import KernelRuntime


def _config() -> Config:
    return Config.model_validate({"agent": {"provider": "local", "model": "m"}})


def _target(cls: type) -> str:
    return f"{__name__}:{cls.__name__}"


class DupProviderA(Service):
    provide = "dup-service"
    plugin_id = "dup-provider-a"


class DupProviderB(Service):
    provide = "dup-service"
    plugin_id = "dup-provider-b"


class VersionedProvider(Service):
    provide = "versioned-service"
    plugin_id = "versioned-provider"
    version = "1.0.0"


class VersionedConsumer(Plugin):
    plugin_id = "versioned-consumer"
    inject = ("versioned-service",)
    requires_versions: ClassVar[dict[str, str]] = {"versioned-service": ">=2.0"}


class CompatibleConsumer(Plugin):
    plugin_id = "compatible-consumer"
    inject = ("versioned-service",)
    requires_versions: ClassVar[dict[str, str]] = {"versioned-service": ">=1.0"}


class MissingConsumer(Plugin):
    plugin_id = "missing-consumer"
    inject = ("absent-service",)


class LiveConfig(BaseModel):
    log_level: str = "info"
    timeout: int = 5
    model: str = "m"


class LivePlugin(Plugin):
    plugin_id = "live-plugin"
    Config = LiveConfig


class DeclaredConfig(BaseModel):
    label: str = "a"


class DeclaredPlugin(Plugin):
    plugin_id = "declared-plugin"
    Config = DeclaredConfig
    volatile: ClassVar[tuple[str, ...]] = ("label",)


class LegacyHostPlugin:
    """A host-style plugin class (no Plugin base): the KI-019 case."""

    plugin_id = "legacy-host"
    version = "0.3.0"
    provide = "legacy-service"

    def __init__(self, ctx, config):
        self.ctx = ctx
        self.config = config

    def init(self) -> None:
        self.started = True


KERNEL_ROW = PluginRow(id="kernel", plugin="deeppyro.kernel.runtime:KernelRuntime")


def _row(plugin_id: str, cls: type, **kwargs) -> PluginRow:
    return PluginRow(id=plugin_id, plugin=_target(cls), **kwargs)


def _composition(*rows: PluginRow) -> Composition:
    return Composition(rows=rows, origin="test")


# -- registry unit tests ----------------------------------------------------


def _manifest(plugin_id: str, *, provides=(), requires=(), version="1.0.0", constraints=()):
    return CapabilityManifest(
        plugin_id=plugin_id,
        version=version,
        services_provided=tuple(provides),
        services_required=tuple(requires),
        requires_versions=tuple(constraints),
    )


def test_duplicate_provider_issue_names_both_plugins() -> None:
    registry = CapabilityRegistry()
    registry.register(_manifest("alpha", provides=("shared",)), plugin_id="alpha")
    registry.register(_manifest("beta", provides=("shared",)), plugin_id="beta")

    report = registry.report()

    assert report.ok is False
    (issue,) = report.fatal_issues
    assert issue.kind == CapabilityIssueKind.DUPLICATE_PROVIDE
    assert issue.plugin_id == "alpha"
    assert issue.related == ("beta",)
    assert "shared" in issue.message
    assert "alpha" in issue.message and "beta" in issue.message
    assert issue.to_dict()["fatal"] is True


def test_duplicate_provider_in_different_isolate_scope_is_allowed() -> None:
    registry = CapabilityRegistry()
    registry.register(_manifest("alpha", provides=("shared",)), plugin_id="alpha", isolate=("one",))
    registry.register(_manifest("beta", provides=("shared",)), plugin_id="beta", isolate=("two",))

    assert registry.report().ok is True


def test_missing_and_pending_requirements_are_diagnostics() -> None:
    registry = CapabilityRegistry()
    registry.register(_manifest("consumer", requires=("absent",)), plugin_id="consumer")
    registry.register(
        _manifest("pending", requires=("offline",)),
        plugin_id="pending",
        state="disabled",
    )
    registry.register(
        _manifest("offline-provider", provides=("offline",)), plugin_id="offline", state="disabled"
    )

    report = registry.report()

    assert report.ok is True
    kinds = {issue.kind for issue in report.issues}
    assert kinds == {CapabilityIssueKind.MISSING_REQUIRE}
    assert report.missing_dependencies("consumer") == ("absent",)


def test_pending_provider_issue() -> None:
    registry = CapabilityRegistry()
    registry.register(_manifest("consumer", requires=("offline",)), plugin_id="consumer")
    registry.register(_manifest("provider", provides=("offline",)), plugin_id="provider", state="disabled")

    report = registry.report()

    assert report.ok is True
    (issue,) = report.issues
    assert issue.kind == CapabilityIssueKind.PENDING_PROVIDER
    assert issue.related == ("provider",)


def test_version_mismatch_is_fatal() -> None:
    registry = CapabilityRegistry()
    registry.register(_manifest("provider", provides=("svc",), version="1.0.0"), plugin_id="provider")
    registry.register(
        _manifest("consumer", requires=("svc",), constraints=(ServiceRequirement("svc", ">=2.0"),)),
        plugin_id="consumer",
    )

    report = registry.report()

    assert report.ok is False
    (issue,) = report.fatal_issues
    assert issue.kind == CapabilityIssueKind.VERSION_MISMATCH
    assert ">=2.0" in issue.message
    assert "provider 1.0.0" in issue.message


def test_dependency_tree_marks_unresolved_requirements() -> None:
    registry = CapabilityRegistry()
    registry.register(_manifest("provider", provides=("svc",)), plugin_id="provider")
    registry.register(
        _manifest("consumer", requires=("svc", "absent")),
        plugin_id="consumer",
    )

    tree = registry.report().dependency_tree("consumer")

    assert tree["pluginId"] == "consumer"
    by_service = {node["service"]: node for node in tree["requires"]}
    assert by_service["svc"]["provider"] == "provider"
    assert by_service["svc"]["satisfied"] is True
    assert by_service["absent"]["provider"] is None
    assert by_service["absent"]["satisfied"] is False


# -- boot-time integration --------------------------------------------------


async def test_boot_rejects_duplicate_providers_with_both_ids() -> None:
    composition = _composition(
        _row("dup-provider-a", DupProviderA),
        _row("dup-provider-b", DupProviderB),
    )

    with pytest.raises(KernelBootError) as excinfo:
        await create_app(_config(), composition=composition)

    message = str(excinfo.value)
    assert "capability validation failed" in message
    assert "dup-service" in message
    assert "dup-provider-a" in message
    assert "dup-provider-b" in message


async def test_boot_rejects_unsatisfied_version_constraint() -> None:
    composition = _composition(
        _row("versioned-provider", VersionedProvider),
        _row("versioned-consumer", VersionedConsumer),
    )

    with pytest.raises(KernelBootError) as excinfo:
        await create_app(_config(), composition=composition)

    message = str(excinfo.value)
    assert "versioned-service" in message
    assert ">=2.0" in message
    assert "versioned-provider 1.0.0" in message


async def test_boot_accepts_satisfied_version_constraint() -> None:
    composition = _composition(
        _row("versioned-provider", VersionedProvider),
        _row("compatible-consumer", CompatibleConsumer),
    )
    app = await create_app(_config(), composition=composition)
    try:
        report = app.capability_report()
        assert report.ok is True
        assert report.fatal_issues == ()
        entry = report.entry("compatible-consumer")
        assert entry is not None
        assert entry.manifest.version_requirement("versioned-service") == ">=1.0"
    finally:
        await app.stop()


async def test_missing_requirement_leaves_dependent_pending() -> None:
    app = await create_app(_config(), composition=_composition(_row("missing-consumer", MissingConsumer)))
    try:
        report = app.capability_report()
        assert report.ok is True
        (issue,) = [item for item in report.issues if item.plugin_id == "missing-consumer"]
        assert issue.kind == CapabilityIssueKind.MISSING_REQUIRE
        assert report.missing_dependencies("missing-consumer") == ("absent-service",)
        assert app.started["missing-consumer"] == "PENDING"
    finally:
        await app.stop()


async def test_host_style_mount_registers_runtime_manifest() -> None:
    app = await create_app(_config(), composition=_composition())
    try:
        fiber = app.ctx.mount(
            {
                "name": "host-dict",
                "apply": lambda ctx, config: None,
                "provides": "host-service",
                "version": "2.1.0",
            }
        )
        await fiber

        report = app.capability_report()
        entry = report.entry("host-dict")
        assert entry is not None
        assert entry.state == ENTRY_RUNTIME
        assert entry.manifest.version == "2.1.0"
        assert entry.manifest.provides == ("host-service",)
        assert report.provider("host-service") is entry
    finally:
        await app.stop()


async def test_host_style_class_mount_registers_runtime_manifest() -> None:
    app = await create_app(_config(), composition=_composition())
    try:
        fiber = app.ctx.mount(LegacyHostPlugin)
        await fiber

        entry = app.capability_report().entry("legacy-host")
        assert entry is not None
        assert entry.state == ENTRY_RUNTIME
        assert entry.manifest.version == "0.3.0"
        assert entry.manifest.provides == ("legacy-service",)
        assert entry.manifest.metadata["qualname"].endswith("LegacyHostPlugin")
    finally:
        await app.stop()


async def test_capability_dict_is_json_serializable() -> None:
    app = await create_app(_config(), composition=_composition(_row("missing-consumer", MissingConsumer)))
    try:
        payload = app.capability_dict()
        assert payload["ok"] is True
        ids = {item["pluginId"] for item in payload["manifests"]}
        assert "missing-consumer" in ids
        assert json.loads(json.dumps(payload))["ok"] is True
    finally:
        await app.stop()


async def test_composition_entries_carry_state_and_origin() -> None:
    composition = _composition(_row("live-plugin", LivePlugin, config={"log_level": "info"}))
    registry = CapabilityRegistry()
    registry.index_composition(composition)

    (entry,) = registry.entries
    assert entry.plugin_id == "live-plugin"
    assert entry.state == ENTRY_ENABLED
    assert entry.origin == "test"
    assert entry.active is False
    assert entry.manifest.config_schema is not None


# -- live config whitelist (KI-021a) ----------------------------------------


async def test_kernel_hot_applies_whitelisted_config_keys() -> None:
    rows = (
        KERNEL_ROW,
        _row(
            "live-plugin",
            LivePlugin,
            config={"log_level": "info", "timeout": 5, "model": "m"},
        ),
    )
    app = await create_app(_config(), composition=Composition(rows=rows, origin="test"))
    try:
        kernel = app.require(KernelRuntime)
        assert kernel.apply_plugin_config("live-plugin", {"log_level": "debug"}) is True
        assert kernel.apply_plugin_config("live-plugin", {"timeout": 9}) is True
        assert "live-plugin" not in app.restart_required

        assert kernel.apply_plugin_config("live-plugin", {"model": "other"}) is False
        assert "live-plugin" in app.restart_required
    finally:
        await app.stop()


async def test_declared_volatile_still_wins() -> None:
    rows = (KERNEL_ROW, _row("declared-plugin", DeclaredPlugin, config={"label": "a"}))
    app = await create_app(_config(), composition=Composition(rows=rows, origin="test"))
    try:
        kernel = app.require(KernelRuntime)
        assert kernel.apply_plugin_config("declared-plugin", {"label": "b"}) is True
        assert "declared-plugin" not in app.restart_required
    finally:
        await app.stop()


async def test_settings_service_hot_applies_whitelisted_keys(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KI-021a end to end: the settings service hands live fields to the kernel."""
    from deeppyro.kernel.manifest import PluginSpec
    from deeppyro.plugins import save_user_manifest

    home = tmp_path / "home"
    monkeypatch.setenv("DEEPPYRO_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    save_user_manifest(
        home / "plugins.toml",
        [PluginSpec(id="live-plugin", name=_target(LivePlugin))],
    )
    settings_target = "deeppyro.builtin.settings:SettingsPlugin"
    rows = (
        KERNEL_ROW,
        _row("live-plugin", LivePlugin, config={"log_level": "info", "model": "m"}),
        PluginRow(id="settings", plugin=settings_target),
    )
    app = await create_app(_config(), composition=Composition(rows=rows, origin="test"))
    try:
        settings = app.require(SettingsPlugin)
        revision = settings.read("live-plugin")["revision"]

        written = settings.write("live-plugin", {"log_level": "debug"}, revision)
        assert written["live_changed"] == ["log_level"]
        assert written["restart_fields"] == []
        assert written["applied_live"] is True
        assert written["restart_required"] is False
        assert written["apply"] == "live"
        record = app.mount_record("live-plugin")
        assert record is not None
        assert record.instance is not None
        assert record.instance.config.log_level == "debug"
        assert "live-plugin" not in app.restart_required

        written = settings.write("live-plugin", {"model": "other"}, written["revision"])
        assert written["live_changed"] == []
        assert written["restart_fields"] == ["model"]
        assert written["applied_live"] is False
        assert written["restart_required"] is True
        assert written["apply"] == "next-boot"
    finally:
        await app.stop()
