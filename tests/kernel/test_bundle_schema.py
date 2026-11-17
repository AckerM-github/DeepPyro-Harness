"""Tests for the pydantic bundle.toml schema used by the loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.api import API_VERSION
from deeppyro.kernel.composition import CompositionError, load_bundle

HEADER = """[bundle]
id = "test"
name = "Test"
version = "0.1.0"

"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _bundle(tmp_path: Path, body: str, *, header: str = HEADER) -> Path:
    return _write(tmp_path, "bundle.toml", header + body)


ROW = """[[plugins]]
id = "ordered"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "schema" }
"""


def test_omitted_api_version_defaults_to_the_supported_version(tmp_path: Path) -> None:
    bundle = load_bundle(_bundle(tmp_path, ROW))
    assert bundle.api_version == API_VERSION == 3
    assert [row.id for row in bundle.rows] == ["ordered"]


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = _bundle(tmp_path, ROW + "\n[extra]\nkey = 1\n")
    with pytest.raises(CompositionError, match="unknown top-level keys: extra"):
        load_bundle(path)


def test_unknown_bundle_table_key_is_rejected(tmp_path: Path) -> None:
    header = """[bundle]
id = "test"
wat = true

"""
    with pytest.raises(CompositionError, match=r"unknown \[bundle\] keys: wat"):
        load_bundle(_bundle(tmp_path, ROW, header=header))


def test_unknown_plugin_row_key_is_rejected(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "ordered"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
sandbox = "danger-full-access"
"""
    with pytest.raises(CompositionError, match="unknown plugin row keys: sandbox"):
        load_bundle(_bundle(tmp_path, body))


def test_row_types_are_validated(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "ordered"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
disabled = "yes"
"""
    with pytest.raises(CompositionError, match="disabled"):
        load_bundle(_bundle(tmp_path, body))


def test_api_version_below_v3_is_rejected_with_a_migration_hint(tmp_path: Path) -> None:
    header = """[bundle]
id = "test"
api_version = 2

"""
    with pytest.raises(CompositionError, match="api_version 2") as excinfo:
        load_bundle(_bundle(tmp_path, ROW, header=header))
    message = str(excinfo.value)
    assert "no compatibility shim" in message
    assert "API v3" in message


def test_api_version_above_v3_is_rejected(tmp_path: Path) -> None:
    header = """[bundle]
id = "test"
api_version = 4

"""
    with pytest.raises(CompositionError, match="api_version 4"):
        load_bundle(_bundle(tmp_path, ROW, header=header))


def test_unknown_requirement_key_is_rejected(tmp_path: Path) -> None:
    header = """[bundle]
id = "test"
requires = [{ id = "core", version = ">=0.1", optional = true }]

"""
    with pytest.raises(CompositionError, match="unknown bundle requirement keys: optional"):
        load_bundle(_bundle(tmp_path, ROW, header=header))


def test_nested_group_rows_are_validated_recursively(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "group"

[[plugins.rows]]
id = "child"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
unexpected = 1
"""
    with pytest.raises(CompositionError, match="unknown plugin row keys: unexpected"):
        load_bundle(_bundle(tmp_path, body))


def test_included_rows_are_validated_too(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "extra.toml",
        """[[plugins]]
id = "inc"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
extra = true
""",
    )
    body = """[[plugins]]
id = "include"
include = "extra.toml"
"""
    with pytest.raises(CompositionError, match="unknown plugin row keys: extra"):
        load_bundle(_bundle(tmp_path, body))
