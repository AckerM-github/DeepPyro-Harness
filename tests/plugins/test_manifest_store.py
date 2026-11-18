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


COMMENTED_MANIFEST = """# User plugin layer: keep this header.

[[plugins]]  # alpha holds the shared library
id = "alpha"
name = "demo.alpha:AlphaPlugin"
config = { level = "info" }  # tune here

# beta is off while migrating
[[plugins]]
id = "beta"
name = "demo.beta:BetaPlugin"
disabled = true
"""


def _commented_specs() -> list[PluginSpec]:
    return [
        PluginSpec(id="alpha", name="demo.alpha:AlphaPlugin", config={"level": "info"}),
        PluginSpec(id="beta", name="demo.beta:BetaPlugin", disabled=True),
    ]


def test_save_preserves_comments_when_patching_values(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text(COMMENTED_MANIFEST, encoding="utf-8")
    specs = [
        PluginSpec(id="alpha", name="demo.alpha:AlphaPlugin", config={"level": "debug"}),
        PluginSpec(id="beta", name="demo.beta:BetaPlugin", disabled=True),
    ]

    save_user_manifest(path, specs)

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# User plugin layer: keep this header.\n")
    assert "# alpha holds the shared library" in text
    assert 'config = {level = "debug"}  # tune here' in text  # comment survives, value changed
    assert "# beta is off while migrating" in text
    assert read_manifest(path) == tuple(specs)


def test_save_is_byte_stable_when_nothing_changes(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text(COMMENTED_MANIFEST, encoding="utf-8")

    save_user_manifest(path, _commented_specs())

    assert path.read_text(encoding="utf-8") == COMMENTED_MANIFEST


def test_save_removes_an_entry_and_keeps_other_comments(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text(COMMENTED_MANIFEST, encoding="utf-8")
    beta = PluginSpec(id="beta", name="demo.beta:BetaPlugin", disabled=True)

    save_user_manifest(path, [beta])

    text = path.read_text(encoding="utf-8")
    assert text.count("[[plugins]]") == 1
    assert "# User plugin layer: keep this header." in text
    assert "# beta is off while migrating" in text
    assert "alpha" not in text
    assert read_manifest(path) == (beta,)


def test_save_appends_new_entry_in_spec_order(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text(COMMENTED_MANIFEST, encoding="utf-8")
    specs = [*_commented_specs(), PluginSpec(id="gamma", name="demo.gamma:GammaPlugin", before="llm")]

    save_user_manifest(path, specs)

    text = path.read_text(encoding="utf-8")
    assert "# User plugin layer: keep this header." in text
    assert 'id = "gamma"' in text and 'before = "llm"' in text
    assert [spec.id for spec in read_manifest(path)] == ["alpha", "beta", "gamma"]
    assert text.index('id = "alpha"') < text.index('id = "beta"') < text.index('id = "gamma"')


def test_save_reorders_blocks_with_their_comments(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text(COMMENTED_MANIFEST, encoding="utf-8")
    specs = list(reversed(_commented_specs()))

    save_user_manifest(path, specs)

    text = path.read_text(encoding="utf-8")
    assert text.index('id = "beta"') < text.index('id = "alpha"')
    assert text.index("# beta is off while migrating") < text.index('id = "alpha"')
    assert [spec.id for spec in read_manifest(path)] == ["beta", "alpha"]


def test_save_keeps_unknown_keys_inside_a_block(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text(
        '[[plugins]]\nid = "alpha"\nname = "demo.alpha:Alpha"\nnote = "custom"\n',
        encoding="utf-8",
    )
    spec = PluginSpec(id="alpha", name="demo.alpha:Alpha", config={"x": 1})

    save_user_manifest(path, [spec])

    text = path.read_text(encoding="utf-8")
    assert 'note = "custom"' in text
    assert read_manifest(path) == (spec,)


def test_save_falls_back_to_full_rewrite_for_invalid_document(tmp_path) -> None:
    path = tmp_path / "plugins.toml"
    path.write_text("garbage = =\n", encoding="utf-8")
    spec = PluginSpec(id="alpha", name="demo.alpha:Alpha")

    save_user_manifest(path, [spec])

    text = path.read_text(encoding="utf-8")
    assert text.startswith("[[plugins]]")
    assert read_manifest(path) == (spec,)
