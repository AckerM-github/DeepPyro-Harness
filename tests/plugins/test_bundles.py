"""Tests for the user bundle lifecycle (Layer D)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.kernel.manifest import PluginSpec
from deeppyro.plugins import (
    BUNDLE_STATE_DISABLED,
    BUNDLE_STATE_FAILED,
    BUNDLE_STATE_LOADED,
    BUNDLE_STATE_OVERRIDDEN,
    BUNDLE_STATE_REMOVED,
    BundleStore,
    PluginError,
    PluginManager,
    PluginNotFoundError,
    PluginProtectionError,
    read_manifest,
    save_user_manifest,
)
from tests.plugins.support import toml_string


def write_bundle(
    directory: Path,
    *,
    bundle_id: str = "demo-bundle",
    name: str = "Demo Bundle",
    version: str = "1.0.0",
    order: int = 500,
    api_version: int = 1,
    critical: bool = False,
    rows: tuple[tuple[str, str], ...] = (("demo-plugin", "demo_pkg:DemoPlugin"),),
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    critical_line = "critical = true\n" if critical else "critical = false\n"
    lines = [
        "[bundle]",
        f"id = {toml_string(bundle_id)}",
        f"name = {toml_string(name)}",
        f"version = {toml_string(version)}",
        f"api_version = {api_version}",
        f"order = {order}",
        critical_line.rstrip("\n"),
        "",
    ]
    for row_id, target in rows:
        lines.extend(
            [
                "[[plugins]]",
                f"id = {toml_string(row_id)}",
                f"plugin = {toml_string(target)}",
                "",
            ]
        )
    (directory / "bundle.toml").write_text("\n".join(lines), encoding="utf-8")
    return directory


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BundleStore:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    return BundleStore()


def test_list_empty_when_no_bundles(store: BundleStore) -> None:
    assert store.list() == ()
    assert not store.root.exists()


def test_install_copies_bundle_and_reports_restart(tmp_path: Path, store: BundleStore) -> None:
    source = write_bundle(tmp_path / "source")
    info = store.install(source)

    assert info.id == "demo-bundle"
    assert info.name == "Demo Bundle"
    assert info.version == "1.0.0"
    assert info.state == BUNDLE_STATE_LOADED
    assert info.enabled is True
    assert info.row_ids == ("demo-plugin",)
    assert info.restart_required is True
    assert (store.root / "demo-bundle" / "bundle.toml").is_file()
    assert not [path for path in store.root.iterdir() if path.name.startswith(".install-")]


def test_install_accepts_bundle_toml_path(tmp_path: Path, store: BundleStore) -> None:
    source = write_bundle(tmp_path / "source")
    info = store.install(source / "bundle.toml")
    assert info.id == "demo-bundle"
    assert (store.root / "demo-bundle" / "bundle.toml").is_file()


def test_install_requires_force_to_replace(tmp_path: Path, store: BundleStore) -> None:
    source = write_bundle(tmp_path / "source", version="1.0.0")
    store.install(source)
    with pytest.raises(PluginError, match="already installed"):
        store.install(source)

    write_bundle(source, version="2.0.0")
    info = store.install(source, force=True)
    assert info.version == "2.0.0"
    assert [row.version for row in store.list()] == ["2.0.0"]


@pytest.mark.parametrize("bundle_id", ["../evil", "a/b", "a\\\\b", "C:evil", ""])
def test_install_rejects_unsafe_bundle_ids(tmp_path: Path, store: BundleStore, bundle_id: str) -> None:
    source = write_bundle(tmp_path / "source", bundle_id=bundle_id)
    with pytest.raises(PluginError):
        store.install(source)
    assert not (store.root.parent / "evil").exists()


def test_install_rejects_malformed_bundle(tmp_path: Path, store: BundleStore) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "bundle.toml").write_text("[bundle]\nid = 'x'\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(PluginError, match="unknown"):
        store.install(source)
    assert not (store.root / "x").exists()


def test_install_missing_source_raises(tmp_path: Path, store: BundleStore) -> None:
    with pytest.raises(PluginError, match="not found"):
        store.install(tmp_path / "missing")


def test_disable_and_enable_round_trip(tmp_path: Path, store: BundleStore) -> None:
    store.install(write_bundle(tmp_path / "source"))

    disabled = store.set_enabled("demo-bundle", False)
    assert disabled.enabled is False
    assert disabled.state == BUNDLE_STATE_DISABLED
    assert disabled.disabled_rows == ("demo-plugin",)
    assert disabled.restart_required is True
    specs = read_manifest(store.manifest_path)
    assert [(spec.id, spec.disabled) for spec in specs] == [("demo-plugin", True)]
    assert store.list()[0].state == BUNDLE_STATE_DISABLED

    repeated = store.set_enabled("demo-bundle", False)
    assert repeated.restart_required is False

    enabled = store.set_enabled("demo-bundle", True)
    assert enabled.enabled is True
    assert enabled.state == BUNDLE_STATE_LOADED
    assert enabled.restart_required is True
    assert not store.manifest_path.exists()

    repeated_enable = store.set_enabled("demo-bundle", True)
    assert repeated_enable.restart_required is False


def test_toggle_preserves_manifest_config(tmp_path: Path, store: BundleStore) -> None:
    store.install(write_bundle(tmp_path / "source"))
    save_user_manifest(store.manifest_path, [PluginSpec(id="demo-plugin", config={"host": "x"})])

    store.set_enabled("demo-bundle", False)
    spec = read_manifest(store.manifest_path)[0]
    assert spec.config == {"host": "x"}
    assert spec.disabled is True

    store.set_enabled("demo-bundle", True)
    spec = read_manifest(store.manifest_path)[0]
    assert spec.config == {"host": "x"}
    assert spec.disabled is None
    assert store.manifest_path.exists()


def test_toggle_protects_builtin_rows(tmp_path: Path, store: BundleStore) -> None:
    source = write_bundle(
        tmp_path / "source",
        rows=(("llm", "deeppyro.builtin.llm:LlmPlugin"),),
    )
    store.install(source)
    with pytest.raises(PluginProtectionError, match="built-in"):
        store.set_enabled("demo-bundle", False)
    assert not store.manifest_path.exists()
    assert store.list()[0].enabled is True


def test_unknown_bundle_raises(store: BundleStore) -> None:
    with pytest.raises(PluginNotFoundError):
        store.set_enabled("missing", False)
    with pytest.raises(PluginNotFoundError):
        store.remove("missing")


def test_remove_deletes_bundle_and_manifest_rows(tmp_path: Path, store: BundleStore) -> None:
    store.install(write_bundle(tmp_path / "source"))
    store.set_enabled("demo-bundle", False)
    assert store.manifest_path.exists()

    info = store.remove("demo-bundle")
    assert info.state == BUNDLE_STATE_REMOVED
    assert info.enabled is False
    assert info.restart_required is True
    assert not (store.root / "demo-bundle").exists()
    assert not store.manifest_path.exists()


def test_remove_keeps_rows_shared_with_other_bundles(tmp_path: Path, store: BundleStore) -> None:
    store.install(
        write_bundle(
            tmp_path / "one",
            bundle_id="one",
            order=500,
            rows=(("shared-row", "pkg:One"), ("one-row", "pkg:One")),
        )
    )
    store.install(
        write_bundle(
            tmp_path / "two",
            bundle_id="two",
            order=600,
            rows=(("shared-row", "pkg:Two"),),
        )
    )
    store.set_enabled("one", False)
    store.set_enabled("two", False)
    store.remove("one")

    ids = {spec.id for spec in read_manifest(store.manifest_path)}
    assert "one-row" not in ids
    assert "shared-row" in ids

    store.remove("two")
    assert not store.manifest_path.exists()


def test_remove_keeps_protected_manifest_rows(tmp_path: Path, store: BundleStore) -> None:
    save_user_manifest(store.manifest_path, [PluginSpec(id="llm", config={"note": "keep"})])
    store.install(
        write_bundle(
            tmp_path / "source",
            rows=(("llm", "deeppyro.builtin.llm:LlmPlugin"), ("demo-plugin", "pkg:Demo")),
        )
    )
    store.remove("demo-bundle")
    specs = read_manifest(store.manifest_path)
    assert [spec.id for spec in specs] == ["llm"]
    assert specs[0].config == {"note": "keep"}


def test_failed_bundle_listed_with_error(tmp_path: Path, store: BundleStore) -> None:
    broken = store.root / "broken"
    broken.mkdir(parents=True)
    (broken / "bundle.toml").write_text("[bundle\nid = 'x'\n", encoding="utf-8")

    infos = store.list()
    assert len(infos) == 1
    assert infos[0].id == "broken"
    assert infos[0].state == BUNDLE_STATE_FAILED
    assert infos[0].error

    removed = store.remove("broken")
    assert removed.state == BUNDLE_STATE_REMOVED
    assert not broken.exists()


def test_overridden_rows_follow_bundle_order(tmp_path: Path, store: BundleStore) -> None:
    store.install(
        write_bundle(
            tmp_path / "early",
            bundle_id="early",
            order=500,
            rows=(("shared-row", "pkg:Early"),),
        )
    )
    store.install(
        write_bundle(
            tmp_path / "late",
            bundle_id="late",
            order=600,
            rows=(("shared-row", "pkg:Late"),),
        )
    )
    by_id = {info.id: info for info in store.list()}
    assert by_id["early"].state == BUNDLE_STATE_OVERRIDDEN
    assert by_id["early"].overridden_rows == ("shared-row",)
    assert by_id["late"].state == BUNDLE_STATE_LOADED
    assert by_id["late"].overridden_rows == ()


def test_manager_bundle_facade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    manager = PluginManager()
    assert manager.bundles_root.name == "bundles"
    assert manager.list_bundles() == []

    source = write_bundle(tmp_path / "source")
    installed = manager.install_bundle(source)
    assert installed.id == "demo-bundle"
    assert [info.id for info in manager.list_bundles()] == ["demo-bundle"]
    assert manager.set_bundle("demo-bundle", False).enabled is False
    assert manager.remove_bundle("demo-bundle").state == BUNDLE_STATE_REMOVED
    assert manager.list_bundles() == []
