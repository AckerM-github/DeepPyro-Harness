"""Tests for the settings store: schema, masking, CAS and persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.api.errors import PluginConfigError
from deeppyro.kernel.manifest import PluginSpec
from deeppyro.plugins import (
    MASKED_VALUE,
    PluginError,
    PluginNotFoundError,
    SettingsConflict,
    SettingsStore,
    manifest_revision,
    mask_secrets,
    plugin_config_schema,
    read_manifest,
    save_user_manifest,
    validate_config,
)
from tests.plugins.support import write_plugin

TARGET = "tests.builtin.demo_plugins:DemoPlugin"

DIR_PLUGIN_MODULE = """
from pydantic import BaseModel
from deeppyro.api import Plugin


class DirConfig(BaseModel):
    apiKey: str = ""
    endpoint: str = "http://localhost"


class DirPlugin(Plugin):
    plugin_id = "dir-plugin"
    Config = DirConfig
"""


def seed(path: Path, *specs: PluginSpec) -> Path:
    save_user_manifest(path, list(specs))
    return path


@pytest.fixture()
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    path = tmp_path / "plugins.toml"
    seed(path, PluginSpec(id="demo-plugin", name=TARGET, config={"apiKey": "hunter2", "maxTokens": 1234}))
    return path


def test_describe_schema_and_masked_values(manifest: Path) -> None:
    described = SettingsStore(manifest_path=manifest).describe("demo-plugin")

    assert described["plugin_id"] == "demo-plugin"
    assert described["target"] == TARGET
    assert described["config_source"] == "manifest"
    # Manifest rows are applied as critical patches by the kernel.
    assert described["critical"] is True
    assert described["disabled"] is False
    assert described["error"] == ""
    assert described["apply"] == "next-boot"
    properties = described["schema"]["properties"]
    assert {"apiKey", "model", "maxTokens", "temperature", "token", "nested"} <= set(properties)
    # maxTokens is masked too: its name contains the "token" fragment.
    assert described["values"] == {"apiKey": MASKED_VALUE, "maxTokens": MASKED_VALUE}
    assert described["volatile_fields"] == ["apiKey", "maxTokens"]
    assert len(described["revision"]) == 16


def test_read_returns_revision_and_masking(manifest: Path) -> None:
    store = SettingsStore(manifest_path=manifest)
    read = store.read("demo-plugin")
    assert read["plugin_id"] == "demo-plugin"
    assert read["values"]["apiKey"] == MASKED_VALUE
    assert read["values"]["maxTokens"] == MASKED_VALUE
    assert read["revision"] == store.revision
    assert read["apply"] == "next-boot"


def test_write_merges_patch_and_preserves_secrets(manifest: Path) -> None:
    store = SettingsStore(manifest_path=manifest)
    before = store.read("demo-plugin")

    written = store.write("demo-plugin", {"temperature": 0.7}, before["revision"])

    assert written["changed"] == ["temperature"]
    assert written["values"]["temperature"] == 0.7
    assert written["values"]["apiKey"] == MASKED_VALUE
    assert written["restart_required"] is True
    assert written["revision"] != before["revision"]

    specs = read_manifest(manifest)
    assert len(specs) == 1
    assert specs[0].id == "demo-plugin"
    assert specs[0].name == TARGET
    assert specs[0].config == {"apiKey": "hunter2", "maxTokens": 1234, "temperature": 0.7}


def test_write_rejects_stale_revision(manifest: Path) -> None:
    store = SettingsStore(manifest_path=manifest)
    revision = store.read("demo-plugin")["revision"]
    store.write("demo-plugin", {"maxTokens": 1}, revision)

    with pytest.raises(SettingsConflict, match="stale settings revision"):
        store.write("demo-plugin", {"maxTokens": 2}, revision)


def test_write_validates_against_config_model(manifest: Path) -> None:
    store = SettingsStore(manifest_path=manifest)
    revision = store.read("demo-plugin")["revision"]
    original = manifest.read_bytes()

    with pytest.raises(PluginConfigError, match="temperature"):
        store.write("demo-plugin", {"temperature": "too-hot"}, revision)

    assert manifest.read_bytes() == original
    assert store.revision == revision


def test_write_rejects_unknown_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    store = SettingsStore(manifest_path=tmp_path / "plugins.toml")
    with pytest.raises(PluginNotFoundError, match="unknown plugin"):
        store.write("nope", {"x": 1}, store.revision)


def test_write_masked_placeholder_keeps_secret(manifest: Path) -> None:
    store = SettingsStore(manifest_path=manifest)
    revision = store.read("demo-plugin")["revision"]

    store.write(
        "demo-plugin",
        {"apiKey": MASKED_VALUE, "model": "other-model"},
        revision,
    )

    config = read_manifest(manifest)[0].config
    assert config["apiKey"] == "hunter2"
    assert config["model"] == "other-model"


def test_write_appends_manifest_entry_for_dir_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    plugins_dir = tmp_path / "plugins"
    write_plugin(
        plugins_dir / "dir-plugin",
        name="Dir Plugin",
        module="dir_plugin_module",
        class_name="DirPlugin",
        module_source=DIR_PLUGIN_MODULE,
    )
    monkeypatch.syspath_prepend(str(plugins_dir))
    manifest = tmp_path / "plugins.toml"
    store = SettingsStore(manifest_path=manifest)

    described = store.describe("dir-plugin")
    assert described["target"] == "dir_plugin_module:DirPlugin"
    assert described["values"] == {}
    assert {"apiKey", "endpoint"} <= set(described["schema"]["properties"])

    written = store.write("dir-plugin", {"apiKey": "secret"}, store.revision)
    assert written["changed"] == ["apiKey"]

    specs = read_manifest(manifest)
    assert len(specs) == 1
    assert specs[0].id == "dir-plugin"
    assert specs[0].name == "dir_plugin_module:DirPlugin"
    assert specs[0].config == {"apiKey": "secret"}


def test_describe_does_not_write_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    manifest = tmp_path / "plugins.toml"
    store = SettingsStore(manifest_path=manifest)

    described = store.describe("llm")

    assert described["target"] == "deeppyro.builtin.llm:LlmPlugin"
    assert described["schema"] is None
    assert described["values"] == {}
    assert described["config_source"] == "bundle"
    assert not manifest.exists()


def test_describe_marks_disabled_manifest_row(manifest: Path) -> None:
    save_user_manifest(
        manifest,
        [PluginSpec(id="demo-plugin", name=TARGET, config={"apiKey": "x"}, disabled=True)],
    )
    described = SettingsStore(manifest_path=manifest).describe("demo-plugin")
    assert described["disabled"] is True


def test_write_keeps_other_manifest_rows(manifest: Path) -> None:
    seed(
        manifest,
        PluginSpec(id="other", name="pkg:Other", config={"keep": True}),
        PluginSpec(id="demo-plugin", name=TARGET, config={"apiKey": "hunter2"}),
    )
    store = SettingsStore(manifest_path=manifest)
    store.write("demo-plugin", {"model": "m2"}, store.revision)

    specs = read_manifest(manifest)
    assert [(spec.id, spec.config) for spec in specs] == [
        ("other", {"keep": True}),
        ("demo-plugin", {"apiKey": "hunter2", "model": "m2"}),
    ]


def test_manifest_revision_tracks_content(tmp_path: Path) -> None:
    path = tmp_path / "plugins.toml"
    absent = manifest_revision(path)
    assert len(absent) == 16

    save_user_manifest(path, [PluginSpec(id="a", config={"x": 1})])
    changed = manifest_revision(path)
    assert changed != absent

    save_user_manifest(path, [PluginSpec(id="a", config={"x": 2})])
    assert manifest_revision(path) != changed


def test_mask_secrets_recurses_and_skips_none() -> None:
    payload = {
        "apiKey": "secret",
        "password": None,
        "nested": {"access_token": "t", "plain": "visible"},
        "items": [{"client_secret": "s"}, {"ok": 1}],
        "model": "local",
    }
    masked = mask_secrets(payload)
    assert masked["apiKey"] == MASKED_VALUE
    assert masked["password"] is None
    assert masked["nested"] == {"access_token": MASKED_VALUE, "plain": "visible"}
    assert masked["items"] == [{"client_secret": MASKED_VALUE}, {"ok": 1}]
    assert masked["model"] == "local"
    assert payload["apiKey"] == "secret"


def test_validate_config_without_config_class() -> None:
    assert plugin_config_schema(None) is None
    validate_config(None, {"anything": object()})


def test_write_requires_mapping_patch(manifest: Path) -> None:
    store = SettingsStore(manifest_path=manifest)
    with pytest.raises(PluginError, match="JSON object"):
        store.write("demo-plugin", ["not", "a", "mapping"], store.revision)  # type: ignore[arg-type]


def test_invalid_manifest_raises_plugin_error(tmp_path: Path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text("not valid toml = =", encoding="utf-8")
    with pytest.raises(PluginError, match="cannot read plugin manifest"):
        SettingsStore(manifest_path=path).describe("demo-plugin")
