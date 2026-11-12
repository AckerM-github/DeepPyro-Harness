"""Tests for external plugin discovery across the three sources."""

from __future__ import annotations

import sys
from pathlib import Path

from deeppyro.plugins import PluginDiscovery
from deeppyro.plugins.model import SOURCE_DIR, SOURCE_ENTRY_POINT, SOURCE_MANIFEST
from tests.plugins.support import (
    FakeDist,
    FakeEntryPoint,
    entry_points_factory,
    write_manifest_text,
    write_plugin,
)

_BUNDLE_IDS = ("llm", "sessions")


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "plugins.toml", tmp_path / "plugins"


def _discovery(
    tmp_path: Path,
    entries: tuple[FakeEntryPoint, ...] = (),
    *,
    manifest_text: str | None = None,
    inject_sys_path: bool = False,
) -> PluginDiscovery:
    manifest, plugins_dir = _paths(tmp_path)
    if manifest_text is not None:
        write_manifest_text(manifest, manifest_text)
    return PluginDiscovery(
        manifest_path=manifest,
        plugins_dir=plugins_dir,
        entry_points=entry_points_factory(entries),
        bundle_ids=_BUNDLE_IDS,
        inject_sys_path=inject_sys_path,
    )


def test_scan_directories(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    write_plugin(
        plugins_dir / "alpha",
        name="Alpha Plugin",
        module="discovery_alpha",
        class_name="AlphaPlugin",
        version="1.2.3",
        description="alpha description",
        provides=("alpha",),
    )
    write_plugin(
        plugins_dir / "beta",
        name="Beta Plugin",
        module="discovery_beta",
        class_name="BetaPlugin",
        provides=None,
    )
    (plugins_dir / "not-a-plugin").mkdir(parents=True)

    rows = _discovery(tmp_path).scan()

    assert [row.id for row in rows] == ["alpha", "beta"]
    alpha, beta = rows
    assert alpha.source == SOURCE_DIR
    assert alpha.name == "Alpha Plugin"
    assert alpha.version == "1.2.3"
    assert alpha.description == "alpha description"
    assert alpha.origin == str(plugins_dir / "alpha" / "plugin.toml")
    assert alpha.target == "discovery_alpha:AlphaPlugin"
    assert alpha.provides == ("alpha",)
    assert alpha.enabled is False
    assert alpha.error is None
    assert beta.provides == ()
    assert beta.description == "a demo plugin"


def test_scan_ignores_directories_without_manifest(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    (plugins_dir / "random").mkdir(parents=True)
    assert _discovery(tmp_path).scan() == ()


def test_scan_missing_plugins_directory_is_empty(tmp_path) -> None:
    assert _discovery(tmp_path).scan() == ()


def test_broken_plugin_toml_is_reported_as_row_error(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    broken = plugins_dir / "broken"
    broken.mkdir(parents=True)
    (broken / "plugin.toml").write_text("module = [\n", encoding="utf-8")
    write_plugin(plugins_dir / "good", module="discovery_good", class_name="GoodPlugin")

    discovery = _discovery(tmp_path)
    rows = discovery.scan()
    by_id = {row.id: row for row in rows}

    assert set(by_id) == {"broken", "good"}
    assert by_id["broken"].source == SOURCE_DIR
    assert by_id["broken"].error is not None
    assert "plugin.toml" in by_id["broken"].error
    assert by_id["good"].error is None


def test_plugin_toml_requires_module_and_class(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    no_module = plugins_dir / "no-module"
    no_module.mkdir(parents=True)
    (no_module / "plugin.toml").write_text('name = "No Module"\n', encoding="utf-8")
    no_class = plugins_dir / "no-class"
    no_class.mkdir(parents=True)
    (no_class / "plugin.toml").write_text('name = "No Class"\nmodule = "bare_module"\n', encoding="utf-8")

    rows = {row.id: row for row in _discovery(tmp_path).scan()}

    assert "module" in rows["no-module"].error
    assert "module:Class" in rows["no-class"].error


def test_scan_entry_points(tmp_path) -> None:
    entry = FakeEntryPoint(
        "entry-plugin",
        "entry_module:EntryPlugin",
        dist=FakeDist(name="entry-dist", version="2.1.0", summary="entry point plugin"),
    )
    broken = FakeEntryPoint("broken-entry", "")
    bare = FakeEntryPoint("bare-module", "bare_module", dist=None)

    rows = {row.id: row for row in _discovery(tmp_path, (entry, broken, bare)).scan()}

    plugin = rows["entry-plugin"]
    assert plugin.source == SOURCE_ENTRY_POINT
    assert plugin.version == "2.1.0"
    assert plugin.origin == "entry-dist==2.1.0"
    assert plugin.description == "entry point plugin"
    assert plugin.target == "entry_module:EntryPlugin"
    assert plugin.error is None
    assert rows["broken-entry"].error is not None
    assert rows["bare-module"].error is None
    assert rows["bare-module"].target is None


def test_scan_entry_points_old_style_dict(tmp_path) -> None:
    entry = FakeEntryPoint("old-entry", "old_module:OldPlugin")
    discovery = PluginDiscovery(
        manifest_path=tmp_path / "plugins.toml",
        plugins_dir=tmp_path / "plugins",
        entry_points=lambda: {"deeppyro.plugins": (entry,)},
        bundle_ids=_BUNDLE_IDS,
    )
    rows = discovery.scan()
    assert [row.id for row in rows] == ["old-entry"]
    assert rows[0].source == SOURCE_ENTRY_POINT


def test_scan_entry_points_plain_list(tmp_path) -> None:
    entry = FakeEntryPoint("list-entry", "list_module:ListPlugin")
    discovery = PluginDiscovery(
        manifest_path=tmp_path / "plugins.toml",
        plugins_dir=tmp_path / "plugins",
        entry_points=lambda: [entry],
        bundle_ids=_BUNDLE_IDS,
    )
    assert [row.id for row in discovery.scan()] == ["list-entry"]


def test_entry_points_from_other_groups_are_skipped(tmp_path) -> None:
    other = FakeEntryPoint("other-entry", "other_module:Other", group="some.other.group")
    discovery = PluginDiscovery(
        manifest_path=tmp_path / "plugins.toml",
        plugins_dir=tmp_path / "plugins",
        entry_points=lambda: [other],
        bundle_ids=_BUNDLE_IDS,
    )
    assert discovery.scan() == ()


def test_duplicate_id_prefers_directory_source(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    write_plugin(plugins_dir / "shared", module="discovery_shared", class_name="SharedPlugin")
    entry = FakeEntryPoint("shared", "entry_shared:SharedPlugin")

    discovery = _discovery(tmp_path, (entry,))
    rows = discovery.scan()

    assert [row.id for row in rows] == ["shared"]
    assert rows[0].source == SOURCE_DIR
    assert any("duplicate" in warning for warning in discovery.warnings)


def test_bundle_ids_are_never_listed(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    write_plugin(plugins_dir / "llm", module="discovery_llm", class_name="LlmPlugin")
    entry = FakeEntryPoint("sessions", "sessions_mod:SessionsPlugin")
    manifest_entry = '[[plugins]]\nid = "llm"\nname = "pkg.mod:Llm"\n'

    discovery = _discovery(tmp_path, (entry,), manifest_text=manifest_entry)
    rows = discovery.scan()

    assert rows == ()
    assert any("built-in bundle" in warning for warning in discovery.warnings)


def test_manifest_entries_are_listed_with_enabled_state(tmp_path) -> None:
    manifest_text = (
        '[[plugins]]\nid = "manifest-one"\nname = "manifest_mod:ManifestPlugin"\n\n'
        '[[plugins]]\nid = "switched-off"\nname = "off_mod:OffPlugin"\ndisabled = true\n'
    )
    discovery = _discovery(tmp_path, manifest_text=manifest_text)
    rows = {row.id: row for row in discovery.scan()}

    one = rows["manifest-one"]
    assert one.source == SOURCE_MANIFEST
    assert one.origin == str(tmp_path / "plugins.toml")
    assert one.target == "manifest_mod:ManifestPlugin"
    assert one.enabled is True
    assert rows["switched-off"].enabled is False


def test_installed_source_dominates_manifest_entry(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    write_plugin(plugins_dir / "alpha", module="discovery_alpha2", class_name="Alpha2")
    manifest_text = '[[plugins]]\nid = "alpha"\nname = "other.mod:Other"\n'

    rows = {row.id: row for row in _discovery(tmp_path, manifest_text=manifest_text).scan()}

    assert rows["alpha"].source == SOURCE_DIR
    assert rows["alpha"].target == "discovery_alpha2:Alpha2"
    assert rows["alpha"].enabled is True


def test_manifest_entry_without_name_is_an_error_row(tmp_path) -> None:
    discovery = _discovery(tmp_path, manifest_text='[[plugins]]\nid = "no-name"\n')
    rows = discovery.scan()
    assert [row.id for row in rows] == ["no-name"]
    assert rows[0].source == SOURCE_MANIFEST
    assert rows[0].error is not None
    assert "'name'" in rows[0].error


def test_manifest_name_must_be_module_class(tmp_path) -> None:
    manifest_text = '[[plugins]]\nid = "alias-only"\nname = "frontend"\n'
    rows = _discovery(tmp_path, manifest_text=manifest_text).scan()
    assert [row.id for row in rows] == ["alias-only"]
    assert rows[0].error is not None
    assert "module:Class" in rows[0].error


def test_invalid_manifest_is_an_error_but_other_sources_survive(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    write_plugin(plugins_dir / "alpha", module="discovery_alpha3", class_name="Alpha3")
    discovery = _discovery(tmp_path, manifest_text="[[plugins]\n")
    rows = discovery.scan()
    assert [row.id for row in rows] == ["alpha"]
    assert rows[0].enabled is False
    assert any("user manifest" in error for error in discovery.errors)


def test_scan_resets_errors_between_runs(tmp_path) -> None:
    discovery = _discovery(tmp_path, manifest_text="[[plugins]\n")
    discovery.scan()
    assert discovery.errors
    discovery.scan()
    assert len(discovery.errors) == 1


def test_inject_sys_path_adds_plugin_directory(tmp_path) -> None:
    _, plugins_dir = _paths(tmp_path)
    write_plugin(plugins_dir / "alpha", module="discovery_path_mod", class_name="PathPlugin")

    discovery = _discovery(tmp_path, inject_sys_path=True)
    discovery.scan()

    assert str(plugins_dir / "alpha") in sys.path
    assert str(plugins_dir / "alpha") == sys.path[0]


def test_default_bundle_ids_come_from_kernel(tmp_path) -> None:
    discovery = PluginDiscovery(
        manifest_path=tmp_path / "plugins.toml",
        plugins_dir=tmp_path / "plugins",
        entry_points=entry_points_factory(()),
        inject_sys_path=False,
    )
    assert "llm" in discovery.bundle_ids
    assert "agents" in discovery.bundle_ids
