"""Tests for declarative `ui.toml` TUI packages."""

from __future__ import annotations

import json
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest

from deeppyro.tui.slots import (
    PANEL,
    SETTINGS,
    STATUS,
    SlotRegistry,
    SlotsPlugin,
    apply_conversation_slots,
    call_slot_factory,
)
from deeppyro.tui.ui_package import (
    default_ui_bundles_root,
    load_installed_ui_packages,
    load_ui_package,
    load_ui_package_report,
)

MODULE_SOURCE = """
def clock(context):
    return "tick"

def is_text(event):
    return event.get("type") == "text"

def row(event):
    return ("row", event.get("text"))

def panel(context):
    return "panel-widget"

def settings(context):
    return "settings-widget"
"""


class FakeConversation:
    def __init__(self) -> None:
        self.widgets: list[Any] = []

    def mount(self, widget: Any) -> None:
        self.widgets.append(widget)


class FakeCtx:
    def __init__(self) -> None:
        self.provided: list[tuple[str, Any]] = []

    def provide(self, name: str, value: Any) -> None:
        self.provided.append((name, value))


def write_bundle(
    root: Path,
    directory: str,
    *,
    bundle_id: str = "",
    ui_toml: str = "",
    module_name: str = "",
    module_source: str = MODULE_SOURCE,
    locale: dict[str, str] | None = None,
) -> Path:
    bundle = root / directory
    bundle.mkdir(parents=True)
    if bundle_id:
        (bundle / "bundle.toml").write_text(
            f'[bundle]\nid = "{bundle_id}"\nname = "Test"\nversion = "0.1.0"\napi_version = 1\n',
            encoding="utf-8",
        )
    (bundle / "ui.toml").write_text(ui_toml, encoding="utf-8")
    if module_name:
        (bundle / f"{module_name}.py").write_text(module_source, encoding="utf-8")
    if locale is not None:
        locale_dir = bundle / "locale"
        locale_dir.mkdir()
        (locale_dir / "en.json").write_text(json.dumps(locale), encoding="utf-8")
    return bundle


def unique_module(prefix: str = "ui_demo") -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def cleanup_modules() -> Any:
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith(("ui_demo", "ui_good")):
            sys.modules.pop(name, None)


def test_load_ui_package_registers_all_slot_kinds(tmp_path: Path) -> None:
    module_name = unique_module()
    bundle = write_bundle(
        tmp_path,
        "acme",
        bundle_id="acme",
        module_name=module_name,
        locale={"acme.greeting": "Hello"},
        ui_toml=f"""
[[status]]
id = "clock"
order = 50
renderer = "{module_name}:clock"

[[conversation]]
id = "text-row"
predicate = "{module_name}:is_text"
factory = "{module_name}:row"

[[panels]]
id = "acme-panel"
title = "Acme"
factory = "{module_name}:panel"

[[settings]]
id = "acme-settings"
title = "Acme settings"
factory = "{module_name}:settings"

[theme]
accent = "#7aa2f7"
""",
    )

    registry = SlotRegistry()
    report = load_ui_package_report(bundle, registry)

    assert report.ok, report.errors
    assert report.id == "acme"
    assert report.path == bundle / "ui.toml"
    assert report.slot_ids == ("clock", "text-row", "acme-panel", "acme-settings")
    assert str(bundle) not in sys.path

    clock = registry.get(STATUS, "clock")
    assert clock is not None
    assert clock.plugin_id == "acme"
    assert clock.order == 50
    assert call_slot_factory(clock.factory, {}) == "tick"

    conversation = FakeConversation()
    mounted = apply_conversation_slots(
        conversation,
        {"type": "text", "text": "hi"},
        registry,
    )
    assert mounted == [("row", "hi")]

    panel = registry.get(PANEL, "acme-panel")
    assert panel is not None
    assert panel.title == "Acme"
    assert call_slot_factory(panel.factory, {}) == "panel-widget"
    assert registry.get(SETTINGS, "acme-settings") is not None

    assert registry.theme() == {"accent": "#7aa2f7"}
    assert registry.translate("acme.greeting") == "Hello"
    assert registry.translate("missing", "fallback") == "fallback"


def test_load_ui_package_returns_registered_ids(tmp_path: Path) -> None:
    module_name = unique_module()
    bundle = write_bundle(
        tmp_path,
        "acme",
        bundle_id="acme",
        module_name=module_name,
        ui_toml=f'[[status]]\nid = "clock"\nrenderer = "{module_name}:clock"\n',
    )
    registry = SlotRegistry()
    assert load_ui_package(bundle, registry) == ["clock"]
    assert registry.get(STATUS, "clock") is not None


def test_load_ui_package_isolates_each_broken_entry(tmp_path: Path) -> None:
    module_name = unique_module()
    bundle = write_bundle(
        tmp_path,
        "broken",
        bundle_id="broken",
        module_name=module_name,
        module_source=MODULE_SOURCE + "\nnot_callable = 42\n",
        ui_toml=f"""
[[status]]
id = "good"
renderer = "{module_name}:clock"

[[status]]
id = "missing-target"
renderer = "{module_name}:nope"

[[conversation]]
id = "not-callable"
factory = "{module_name}:not_callable"

[[conversation]]
id = "bad-predicate"
predicate = 12
factory = "{module_name}:row"

[[panels]]
id = "still-good"
factory = "{module_name}:panel"

[[status]]
id = "good"
renderer = "{module_name}:clock"
""",
    )

    registry = SlotRegistry()
    report = load_ui_package_report(bundle, registry)

    assert not report.ok
    assert report.slot_ids == ("good", "still-good")
    assert registry.get(STATUS, "good") is not None
    assert registry.get(PANEL, "still-good") is not None
    joined = "\n".join(report.errors)
    assert "target" in joined and "not found" in joined
    assert "not callable" in joined
    assert "duplicate status slot id" in joined


def test_load_ui_package_survives_bad_toml(tmp_path: Path) -> None:
    bundle = tmp_path / "bad"
    bundle.mkdir()
    (bundle / "ui.toml").write_text('[[status]\nid = "oops"\n', encoding="utf-8")
    registry = SlotRegistry()
    report = load_ui_package_report(bundle, registry)
    assert report.id == "bad"
    assert not report.ok
    assert "cannot read ui.toml" in report.errors[0]
    assert load_ui_package(bundle, registry) == []


def test_load_ui_package_missing_directory(tmp_path: Path) -> None:
    registry = SlotRegistry()
    report = load_ui_package_report(tmp_path / "does-not-exist", registry)
    assert not report.ok
    assert "not found" in report.errors[0]
    assert report.slot_ids == ()


def test_load_ui_package_uses_injected_importer(tmp_path: Path) -> None:
    bundle = write_bundle(
        tmp_path,
        "injected",
        bundle_id="injected",
        ui_toml='[[status]]\nid = "clock"\nrenderer = "fake_pkg:clock"\n',
    )
    module = types.ModuleType("fake_pkg")
    module.clock = lambda context: "injected"  # type: ignore[attr-defined]
    imported: list[str] = []

    def importer(name: str) -> types.ModuleType:
        imported.append(name)
        return module

    registry = SlotRegistry()
    ids = load_ui_package(bundle, registry, import_module=importer)
    assert ids == ["clock"]
    assert imported == ["fake_pkg"]
    assert str(bundle) not in sys.path
    contribution = registry.get(STATUS, "clock")
    assert contribution is not None
    assert call_slot_factory(contribution.factory, {}) == "injected"


def test_load_ui_package_reports_theme_and_locale_errors(tmp_path: Path) -> None:
    bundle = write_bundle(
        tmp_path,
        "bad-meta",
        ui_toml="""
[theme]
accent = 12

[[status]]
id = "broken-theme"
renderer = "missing_module:clock"
""",
        locale={"key": "value"},
    )
    registry = SlotRegistry()
    report = load_ui_package_report(bundle, registry)
    assert not report.ok
    assert report.id == "bad-meta"
    assert any("[theme]" in error for error in report.errors)


def test_load_installed_ui_packages_scans_sorted_and_isolates(tmp_path: Path) -> None:
    root = tmp_path / "bundles"
    good_module = unique_module("ui_good")
    write_bundle(
        root,
        "b-good",
        bundle_id="good",
        module_name=good_module,
        ui_toml=f'[[status]]\nid = "clock"\nrenderer = "{good_module}:clock"\n',
    )
    bad = root / "a-bad"
    bad.mkdir(parents=True)
    (bad / "ui.toml").write_text("not = [valid", encoding="utf-8")
    (root / "c-no-ui").mkdir()

    registry = SlotRegistry()
    reports = load_installed_ui_packages(registry, root=root)

    assert [report.id for report in reports] == ["a-bad", "good"]
    assert reports[0].errors
    assert reports[1].ok
    assert registry.get(STATUS, "clock") is not None
    assert load_installed_ui_packages(registry, root=tmp_path / "missing") == []


def test_default_ui_bundles_root_honours_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path))
    assert default_ui_bundles_root() == tmp_path / "bundles"


def test_slots_plugin_boots_with_ui_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path))
    root = tmp_path / "bundles"
    good_module = unique_module("ui_good")
    write_bundle(
        root,
        "good",
        bundle_id="good",
        module_name=good_module,
        locale={"good.label": "Good"},
        ui_toml=f'[[status]]\nid = "clock"\nrenderer = "{good_module}:clock"\n\n[theme]\naccent = "#fff"\n',
    )
    bad = root / "bad"
    bad.mkdir(parents=True)
    (bad / "ui.toml").write_text("broken = [", encoding="utf-8")

    ctx = FakeCtx()
    plugin = SlotsPlugin(ctx, {})
    plugin.setup(ctx, {})

    assert ctx.provided == [("slots", plugin.registry)]
    assert [report.id for report in plugin.ui_packages] == ["bad", "good"]
    assert plugin.registry.get(STATUS, "clock") is not None
    assert plugin.registry.theme() == {"accent": "#fff"}
    assert plugin.registry.translate("good.label") == "Good"
    assert any("bad" in error for error in plugin.ui_errors)
    assert plugin.ui_summary == "bad: 0 slot(s), 1 error(s); good: 1 slot(s), ok"


def test_slots_plugin_scan_failure_is_not_fatal() -> None:
    ctx = FakeCtx()
    plugin = SlotsPlugin(ctx, {})

    def broken_scan(bundles_dir: str | Path | None = None) -> Any:
        raise RuntimeError("scan exploded")

    plugin.load_ui_packages = broken_scan  # type: ignore[method-assign]
    plugin.setup(ctx, {})
    assert plugin.ui_packages == []
    assert plugin.ui_summary == "no UI packages"
    assert "cannot scan UI packages" in plugin.ui_errors[0]
