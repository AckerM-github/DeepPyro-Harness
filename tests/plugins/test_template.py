"""Tests for the bundle scaffold: template text and create_bundle."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from deeppyro.api import Plugin
from deeppyro.kernel.composition import load_bundle
from deeppyro.plugins import PluginError, bundle_template, create_bundle


def _import_plugin_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("generated_bundle_plugin", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_template_returns_three_files() -> None:
    files = bundle_template("demo-bundle", "Demo Bundle")

    assert set(files) == {"bundle.toml", "plugin.py", "README.md"}
    assert 'id = "demo-bundle"' in files["bundle.toml"]
    assert 'name = "Demo Bundle"' in files["bundle.toml"]
    assert 'plugin = "plugin.py:DemoBundlePlugin"' in files["bundle.toml"]
    assert "class DemoBundlePlugin(Plugin):" in files["plugin.py"]
    assert 'plugin_id = "demo-bundle"' in files["plugin.py"]
    assert "# Demo Bundle" in files["README.md"]


def test_bundle_template_derives_class_from_bundle_id() -> None:
    files = bundle_template("my cool-bundle")

    assert "class MyCoolBundlePlugin(Plugin):" in files["plugin.py"]
    assert 'name = "my cool-bundle"' in files["bundle.toml"]


def test_bundle_template_api_version_is_configurable() -> None:
    files = bundle_template("demo-bundle", api_version=1)

    assert "api_version = 1" in files["bundle.toml"]


@pytest.mark.parametrize("bundle_id", ["", "  ", ".", "..", "a/b", "a\\b", "a:b"])
def test_bundle_template_rejects_bad_ids(bundle_id: str) -> None:
    with pytest.raises(PluginError):
        bundle_template(bundle_id)


def test_create_bundle_writes_loadable_bundle(tmp_path: Path) -> None:
    target = tmp_path / "bundles" / "demo-bundle"

    created = create_bundle(target, "demo-bundle", "Demo Bundle")

    assert created == target
    assert {path.name for path in target.iterdir()} == {"bundle.toml", "plugin.py", "README.md"}
    bundle = load_bundle(target)
    assert bundle.id == "demo-bundle"
    assert bundle.name == "Demo Bundle"
    assert bundle.order == 500
    assert [row.id for row in bundle.rows] == ["demo-bundle"]
    assert bundle.rows[0].plugin == "plugin.py:DemoBundlePlugin"
    assert not any(path.suffix == ".tmp" for path in tmp_path.rglob("*"))


def test_create_bundle_plugin_class_imports(tmp_path: Path) -> None:
    target = create_bundle(tmp_path / "demo-bundle", "demo-bundle")
    sys.path.insert(0, str(target))
    try:
        module = _import_plugin_module(target / "plugin.py")
    finally:
        sys.path.remove(str(target))
    plugin_class = module.DemoBundlePlugin

    assert issubclass(plugin_class, Plugin)
    assert plugin_class.plugin_id == "demo-bundle"
    assert plugin_class.volatile == ("greeting",)
    assert plugin_class.inject == ["tools"]
    assert "greeting" in plugin_class.Config.model_fields


def test_create_bundle_allows_empty_existing_directory(tmp_path: Path) -> None:
    target = tmp_path / "demo-bundle"
    target.mkdir()

    create_bundle(target, "demo-bundle")

    assert (target / "bundle.toml").is_file()


def test_create_bundle_rejects_non_empty_directory(tmp_path: Path) -> None:
    target = tmp_path / "demo-bundle"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(PluginError, match="not empty"):
        create_bundle(target, "demo-bundle")


def test_create_bundle_rejects_file_destination(tmp_path: Path) -> None:
    target = tmp_path / "demo-bundle"
    target.write_text("not a directory", encoding="utf-8")

    with pytest.raises(PluginError, match="not a directory"):
        create_bundle(target, "demo-bundle")


def test_create_bundle_rejects_unsupported_api_version(tmp_path: Path) -> None:
    with pytest.raises(PluginError):
        create_bundle(tmp_path / "demo-bundle", "demo-bundle", api_version=99)
    assert not (tmp_path / "demo-bundle").exists()
