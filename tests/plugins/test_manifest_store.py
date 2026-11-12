"""Tests for the atomic user-manifest reader/writer."""

from __future__ import annotations

import datetime as dt

import pytest

from deeppyro.kernel.manifest import ManifestError, PluginSpec
from deeppyro.plugins.errors import ManifestWriteError
from deeppyro.plugins.manifest_store import dump_manifest, read_manifest, save_user_manifest


def _specs() -> list[PluginSpec]:
    return [
        PluginSpec(
            id="alpha",
            name="demo.alpha:AlphaPlugin",
            config={
                "flag": True,
                "level": 3,
                "ratio": 0.5,
                "tags": ["a", "b"],
                "nested": {"answer": 42},
            },
            before="llm",
        ),
        PluginSpec(id="beta", name="demo.beta:BetaPlugin", disabled=False),
        PluginSpec(id="gamma", name="demo.gamma:GammaPlugin", disabled=True, after="agents"),
    ]


def test_roundtrip_preserves_every_field(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    specs = _specs()
    save_user_manifest(path, specs)
    assert path.is_file()
    assert read_manifest(path) == tuple(specs)


def test_dump_manifest_emits_plugin_tables() -> None:
    text = dump_manifest(_specs())
    assert text.startswith("[[plugins]]")
    assert 'id = "alpha"' in text
    assert "disabled = false" in text
    assert "disabled = true" in text
    assert 'before = "llm"' in text
    assert 'after = "agents"' in text
    assert text.endswith("\n")


def test_empty_manifest_serializes_to_empty_string() -> None:
    assert dump_manifest([]) == ""


def test_save_empty_manifest_removes_existing_file(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text('[[plugins]]\nid = "gone"\nname = "a:B"\n', encoding="utf-8")
    save_user_manifest(path, [])
    assert not path.exists()


def test_save_leaves_no_temporary_files(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    save_user_manifest(path, _specs())
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_creates_parent_directories(tmp_path) -> None:
    path = tmp_path / "nested" / "config" / "plugins.toml"
    save_user_manifest(path, [PluginSpec(id="one", name="demo.one:One")])
    assert path.is_file()
    assert read_manifest(path)[0].id == "one"


def test_unicode_and_quotes_roundtrip(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    specs = [
        PluginSpec(
            id="数据",
            name='插件 "quoted"\n第二行',
            config={"路径": "D:/插件/目录", "quote": "single ' and double \""},
        )
    ]
    save_user_manifest(path, specs)
    assert read_manifest(path) == tuple(specs)


def test_datetime_config_roundtrip(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    specs = [
        PluginSpec(id="clock", name="demo.clock:Clock", config={"start": dt.datetime(2024, 1, 2, 3, 4, 5)})
    ]
    save_user_manifest(path, specs)
    assert read_manifest(path) == tuple(specs)


def test_unsupported_value_raises_write_error() -> None:
    specs = [PluginSpec(id="bad", name="demo.bad:Bad", config={"object": object()})]
    with pytest.raises(ManifestWriteError):
        dump_manifest(specs)


def test_missing_manifest_reads_empty(tmp_path) -> None:
    assert read_manifest(tmp_path / "missing.toml") == ()


def test_invalid_manifest_raises_manifest_error(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text("[[plugins]\n", encoding="utf-8")
    with pytest.raises(ManifestError):
        read_manifest(path)
