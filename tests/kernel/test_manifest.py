from pathlib import Path

import pytest

from deeppyro.kernel.manifest import (
    ManifestError,
    PluginSpec,
    load_manifest,
    merge_plugins,
    resolve_plugin_name,
)


def test_load_manifest_parses_entries(tmp_path: Path) -> None:
    p = tmp_path / "dpyro.toml"
    p.write_text(
        """
[[plugins]]
id = "session"
name = "deeppyro.plugins.session:SessionPlugin"
config = { root = "x" }

[[plugins]]
id = "frontend"
disabled = true
""",
        encoding="utf-8",
    )
    specs = load_manifest(p)
    assert [s.id for s in specs] == ["session", "frontend"]
    assert specs[0].config == {"root": "x"}
    assert specs[1].disabled is True


def test_load_manifest_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path / "nope.toml") == ()


def test_load_manifest_rejects_missing_id(tmp_path: Path) -> None:
    p = tmp_path / "bad.toml"
    p.write_text('[[plugins]]\nname = "x:Y"\n', encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(p)


def test_merge_overrides_config_and_disabled() -> None:
    base = (
        PluginSpec(id="a", name="m:A", config={"x": 1, "y": 2}),
        PluginSpec(id="b", name="m:B"),
    )
    patch = (
        PluginSpec(id="a", config={"y": 3}),
        PluginSpec(id="b", disabled=True),
    )
    merged = merge_plugins(base, patch)
    assert merged[0].config == {"x": 1, "y": 3}
    assert merged[0].name == "m:A"  # patch did not rename
    assert merged[1].disabled is True


def test_merge_inserts_new_plugins_before_after() -> None:
    base = (PluginSpec(id="a", name="m:A"), PluginSpec(id="b", name="m:B"))
    merged = merge_plugins(
        base,
        (
            PluginSpec(id="first", name="m:F", before="a"),
            PluginSpec(id="last", name="m:L", after="b"),
        ),
    )
    assert [s.id for s in merged] == ["first", "a", "b", "last"]


def test_merge_new_plugin_requires_name() -> None:
    with pytest.raises(ManifestError):
        merge_plugins((), (PluginSpec(id="x"),))


def test_merge_unknown_anchor_raises() -> None:
    with pytest.raises(ManifestError):
        merge_plugins((), (PluginSpec(id="x", name="m:X", after="nope"),))


def test_resolve_plugin_name_roundtrip() -> None:
    cls = resolve_plugin_name("tests.kernel.plugins_dummy:CounterPlugin")
    assert cls.__name__ == "CounterPlugin"


def test_resolve_plugin_name_rejects_plain_name() -> None:
    with pytest.raises(ManifestError):
        resolve_plugin_name("counter")
