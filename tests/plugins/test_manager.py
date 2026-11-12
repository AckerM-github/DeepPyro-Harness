"""Tests for the plugin manager: enable, disable, validate and remove."""

from __future__ import annotations

import importlib

import pytest

from deeppyro.kernel.manifest import load_manifest
from deeppyro.plugins import (
    PluginError,
    PluginManager,
    PluginNotFoundError,
    PluginProtectionError,
    PluginRemovalError,
)
from deeppyro.plugins.model import SOURCE_DIR, SOURCE_ENTRY_POINT, SOURCE_MANIFEST
from tests.plugins.support import (
    FakeDist,
    FakeEntryPoint,
    entry_points_factory,
    write_manifest_text,
    write_plugin,
)


def _manager(
    tmp_path,
    entries: tuple[FakeEntryPoint, ...] = (),
    *,
    manifest_text: str | None = None,
) -> PluginManager:
    manifest = tmp_path / "plugins.toml"
    if manifest_text is not None:
        write_manifest_text(manifest, manifest_text)
    return PluginManager(
        manifest_path=manifest,
        plugins_dir=tmp_path / "plugins",
        entry_points=entry_points_factory(entries),
        bundle_ids=("llm", "sessions"),
    )


def test_empty_sources_have_no_plugins(tmp_path) -> None:
    manager = _manager(tmp_path)

    assert manager.list() == []
    assert manager.errors == ()
    assert manager.manifest_path == tmp_path / "plugins.toml"
    assert manager.plugins_dir == tmp_path / "plugins"


def test_list_returns_a_copy(tmp_path) -> None:
    manager = _manager(tmp_path, manifest_text='[[plugins]]\nid = "only"\nname = "pkg.mod:Only"\n')
    rows = manager.list()
    assert [row.id for row in rows] == ["only"]
    rows.clear()
    assert [row.id for row in manager.list()] == ["only"]


def test_validate_directory_plugin(tmp_path) -> None:
    manager = _manager(tmp_path)
    write_plugin(tmp_path / "plugins" / "alpha", module="manager_alpha", class_name="AlphaPlugin")
    manager.refresh()

    result = manager.validate("alpha")

    assert result.ok is True
    assert result.target == "manager_alpha:AlphaPlugin"
    assert result.provides == ("demo",)
    assert result.error is None
    row = manager.get("alpha")
    assert row is not None
    assert row.provides == ("demo",)


def test_validate_reports_import_failure_without_raising(tmp_path) -> None:
    manager = _manager(tmp_path)
    plugin = write_plugin(
        tmp_path / "plugins" / "alpha", module="manager_missing_mod", class_name="MissingPlugin"
    )
    (plugin / "manager_missing_mod.py").unlink()
    manager.refresh()

    result = manager.validate("alpha")

    assert result.ok is False
    assert result.error is not None
    assert "manager_missing_mod" in result.error
    row = manager.get("alpha")
    assert row is not None
    assert row.error is None


def test_enable_writes_manifest_entry(tmp_path) -> None:
    manager = _manager(tmp_path)
    write_plugin(tmp_path / "plugins" / "alpha", module="manager_enable", class_name="EnablePlugin")
    manager.refresh()

    info = manager.enable("alpha")

    assert info.enabled is True
    specs = load_manifest(manager.manifest_path)
    assert len(specs) == 1
    assert specs[0].id == "alpha"
    assert specs[0].name == "manager_enable:EnablePlugin"
    assert specs[0].disabled is None
    assert manager.list()[0].enabled is True


def test_enable_is_idempotent(tmp_path) -> None:
    manager = _manager(tmp_path)
    write_plugin(tmp_path / "plugins" / "alpha", module="manager_idem", class_name="IdemPlugin")
    manager.refresh()

    manager.enable("alpha")
    manager.enable("alpha")

    assert len(load_manifest(manager.manifest_path)) == 1


def test_disable_after_enable_marks_entry_disabled(tmp_path) -> None:
    manager = _manager(tmp_path)
    write_plugin(tmp_path / "plugins" / "alpha", module="manager_toggle", class_name="TogglePlugin")
    manager.refresh()
    manager.enable("alpha")

    info = manager.disable("alpha")

    assert info.enabled is False
    specs = load_manifest(manager.manifest_path)
    assert len(specs) == 1
    assert specs[0].disabled is True
    assert specs[0].name == "manager_toggle:TogglePlugin"
    assert manager.list()[0].enabled is False


def test_disable_manifest_plugin_keeps_entry(tmp_path) -> None:
    manager = _manager(tmp_path, manifest_text='[[plugins]]\nid = "only"\nname = "pkg.mod:Only"\n')

    info = manager.disable("only")

    assert info.enabled is False
    specs = load_manifest(manager.manifest_path)
    assert len(specs) == 1
    assert specs[0].disabled is True
    assert specs[0].name == "pkg.mod:Only"


def test_enable_preserves_existing_config(tmp_path) -> None:
    manifest_text = (
        "[[plugins]]\n"
        'id = "alpha"\n'
        'name = "manager_config:ConfigPlugin"\n'
        "disabled = true\n"
        'after = "agents"\n'
        "config = { level = 2 }\n"
    )
    manager = _manager(tmp_path, manifest_text=manifest_text)
    write_plugin(tmp_path / "plugins" / "alpha", module="manager_config", class_name="ConfigPlugin")
    manager.refresh()

    info = manager.enable("alpha")

    assert info.enabled is True
    specs = load_manifest(manager.manifest_path)
    assert len(specs) == 1
    assert specs[0].disabled is None
    assert specs[0].config == {"level": 2}
    assert specs[0].after == "agents"


def test_enable_import_failure_does_not_write_manifest(tmp_path) -> None:
    manager = _manager(tmp_path)
    plugin = write_plugin(
        tmp_path / "plugins" / "alpha", module="manager_broken_import", class_name="BrokenPlugin"
    )
    (plugin / "manager_broken_import.py").unlink()
    manager.refresh()

    with pytest.raises(PluginError, match="cannot enable"):
        manager.enable("alpha")

    assert not manager.manifest_path.exists()


def test_enable_with_unreadable_manifest_does_not_rewrite(tmp_path) -> None:
    manager = _manager(tmp_path, manifest_text="[[plugins]\n")
    write_plugin(tmp_path / "plugins" / "alpha", module="manager_unreadable", class_name="Unreadable")
    manager.refresh()
    text_before = manager.manifest_path.read_text(encoding="utf-8")

    with pytest.raises(PluginError):
        manager.enable("alpha")

    assert manager.manifest_path.read_text(encoding="utf-8") == text_before


def test_bundle_plugins_are_protected(tmp_path) -> None:
    manager = _manager(tmp_path)

    for action in (manager.enable, manager.disable, manager.remove):
        with pytest.raises(PluginProtectionError):
            action("llm")


def test_unknown_plugin_ids_raise(tmp_path) -> None:
    manager = _manager(tmp_path)

    with pytest.raises(PluginNotFoundError):
        manager.enable("nope")
    with pytest.raises(PluginNotFoundError):
        manager.validate("nope")
    with pytest.raises(PluginNotFoundError):
        manager.remove("nope")
    assert manager.get("nope") is None


def test_remove_directory_plugin(tmp_path) -> None:
    manager = _manager(tmp_path)
    plugin = write_plugin(tmp_path / "plugins" / "alpha", module="manager_remove", class_name="RemovePlugin")
    manager.refresh()
    manager.enable("alpha")

    manager.remove("alpha")

    assert not plugin.exists()
    assert not manager.manifest_path.exists()
    assert manager.list() == []


def test_remove_manifest_only_plugin(tmp_path) -> None:
    manager = _manager(tmp_path, manifest_text='[[plugins]]\nid = "only"\nname = "pkg.mod:Only"\n')

    manager.remove("only")

    assert manager.list() == []
    assert not manager.manifest_path.exists()


def test_remove_entry_point_plugin_is_refused(tmp_path) -> None:
    entry = FakeEntryPoint("ep", "ep_mod:EpPlugin", dist=FakeDist())
    manager = _manager(tmp_path, (entry,))

    with pytest.raises(PluginRemovalError, match="pip"):
        manager.remove("ep")


def test_refresh_detects_new_plugin(tmp_path) -> None:
    manager = _manager(tmp_path)
    assert manager.list() == []

    write_plugin(tmp_path / "plugins" / "late", module="manager_late", class_name="LatePlugin")
    rows = manager.refresh()

    assert [row.id for row in rows] == ["late"]
    assert rows[0].source == SOURCE_DIR


def test_entry_point_validation_and_enable(tmp_path, monkeypatch) -> None:
    module_file = tmp_path / "manager_ep_mod.py"
    module_file.write_text('class EpPlugin:\n    provide = "ep-demo"\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("manager_ep_mod")
    entry = FakeEntryPoint("ep", "manager_ep_mod:EpPlugin", loaded=module.EpPlugin)
    manager = _manager(tmp_path, (entry,))

    result = manager.validate("ep")

    assert result.ok is True
    assert result.target == "manager_ep_mod:EpPlugin"
    assert result.provides == ("ep-demo",)
    info = manager.enable("ep")
    assert info.enabled is True
    assert info.source == SOURCE_ENTRY_POINT
    specs = load_manifest(manager.manifest_path)
    assert len(specs) == 1
    assert specs[0].name == "manager_ep_mod:EpPlugin"


def test_manifest_rows_source_and_origin(tmp_path) -> None:
    manager = _manager(tmp_path, manifest_text='[[plugins]]\nid = "only"\nname = "pkg.mod:Only"\n')
    row = manager.list()[0]
    assert row.source == SOURCE_MANIFEST
    assert row.origin == str(tmp_path / "plugins.toml")
    assert row.enabled is True
