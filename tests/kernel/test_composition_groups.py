"""Tests for group rows, nested children and include files in the composition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deeppyro.config import Config
from deeppyro.kernel.app import create_app
from deeppyro.kernel.composition import (
    Composition,
    CompositionError,
    load_bundle,
    rows_from_specs,
)
from deeppyro.kernel.manifest import load_manifest
from tests.kernel import plugins_dummy

DUMMY = "tests.kernel.plugins_dummy"

BUNDLE_HEADER = """[bundle]
id = "test"
name = "Test layers"
version = "0.1.0"

"""


def _config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "m"},
            "providers": {
                "local": {"api": "openai", "baseURL": "http://127.0.0.1:8000/v1", "models": [{"id": "m"}]}
            },
        }
    )


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _bundle_text(body: str) -> str:
    return BUNDLE_HEADER + body


def _composition(tmp_path: Path, body: str) -> Composition:
    path = _write(tmp_path, "bundle.toml", _bundle_text(body))
    return Composition(load_bundle(path).rows, origin="test")


async def _boot_composition(composition: Composition) -> Any:
    return await create_app(_config(), composition=composition)


def _ordered_row(row_id: str, tag: str) -> str:
    return f"""[[plugins]]
id = "{row_id}"
plugin = "{DUMMY}:OrderedPlugin"
config = {{ tag = "{tag}" }}
"""


def _reset_order() -> None:
    plugins_dummy.MOUNT_ORDER.clear()


async def test_group_mounts_children_in_one_child_context(tmp_path: Path) -> None:
    body = (
        _ordered_row("before-row", "before")
        + """[[plugins]]
id = "group-a"

[[plugins.rows]]
id = "child-a"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "a" }

[[plugins.rows]]
id = "child-b"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "b" }
"""
        + _ordered_row("after-row", "after")
    )
    _reset_order()
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert app.started == {"before-row": "ACTIVE", "group-a": "ACTIVE", "after-row": "ACTIVE"}
        assert plugins_dummy.MOUNT_ORDER == ["before", "a", "b", "after"]
        assert [row.id for row in app.rows] == ["before-row", "group-a", "after-row"]
        assert [state["id"] for state in app.row_states()] == [
            "before-row",
            "group-a",
            "child-a",
            "child-b",
            "after-row",
        ]
        states = {state["id"]: state for state in app.row_states()}
        group = states["group-a"]
        assert group["group"] is True
        assert group["state"] == "active"
        assert group["runtime_state"] == "group"
        assert group["children"] == ["child-a", "child-b"]
        assert states["child-a"]["parent"] == "group-a"
        assert states["child-b"]["parent"] == "group-a"
        assert states["child-a"]["group"] is False
    finally:
        await app.stop()


async def test_group_isolate_mounts_children_that_share_services(tmp_path: Path) -> None:
    plugins_dummy.OBSERVED.clear()
    body = """[[plugins]]
id = "group-isolated"
isolate = ["scope"]

[[plugins.rows]]
id = "counter"
plugin = "tests.kernel.plugins_dummy:CounterPlugin"
config = { start = 7 }

[[plugins.rows]]
id = "probe"
plugin = "tests.kernel.plugins_dummy:InterceptProbePlugin"
"""
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert plugins_dummy.OBSERVED == [7]
        assert app.service("counter").n == 7
        states = {state["id"]: state for state in app.row_states()}
        assert states["group-isolated"]["state"] == "active"
        assert states["probe"]["parent"] == "group-isolated"
    finally:
        await app.stop()


async def test_group_intercept_reaches_child_services(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "group-intercepted"
intercept = { intercept-aware = { mode = "group" } }

[[plugins.rows]]
id = "aware"
plugin = "tests.kernel.plugins_dummy:InterceptAwareService"
"""
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        service = app.service("intercept-aware")
        assert service.resolved == {"mode": "group"}
    finally:
        await app.stop()


async def test_nested_rows_imply_group_without_group_flag(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "implicit-group"

[[plugins.rows]]
id = "implicit-child"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "implicit" }
"""
    _reset_order()
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert [row.id for row in app.rows] == ["implicit-group"]
        states = {state["id"]: state for state in app.row_states()}
        assert states["implicit-group"]["group"] is True
        assert states["implicit-child"]["parent"] == "implicit-group"
        assert plugins_dummy.MOUNT_ORDER == ["implicit"]
    finally:
        await app.stop()


async def test_inline_rows_list_mounts_children(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "group-inline"
group = true
rows = [
  { id = "inline-a", plugin = "tests.kernel.plugins_dummy:OrderedPlugin", config = { tag = "inline-a" } },
  { id = "inline-b", plugin = "tests.kernel.plugins_dummy:OrderedPlugin", config = { tag = "inline-b" } },
]
"""
    _reset_order()
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert plugins_dummy.MOUNT_ORDER == ["inline-a", "inline-b"]
        assert [row.id for row in app.rows] == ["group-inline"]
        states = {state["id"]: state for state in app.row_states()}
        assert states["group-inline"]["children"] == ["inline-a", "inline-b"]
        assert states["inline-a"]["parent"] == "group-inline"
    finally:
        await app.stop()


async def test_disabled_group_skips_all_children(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "group-off"
disabled = true

[[plugins.rows]]
id = "child-off"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "never" }
"""
    _reset_order()
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert plugins_dummy.MOUNT_ORDER == []
        assert app.rows == ()
        assert app.started == {}
        states = {state["id"]: state for state in app.row_states()}
        assert states["group-off"]["state"] == "disabled"
        assert states["child-off"]["state"] == "disabled"
        assert states["child-off"]["parent"] == "group-off"
    finally:
        await app.stop()


async def test_noncritical_group_child_failure_marks_group_failed(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "group-flaky"

[[plugins.rows]]
id = "flaky-child"
plugin = "tests.kernel.plugins_dummy:ExplodingPlugin"
critical = false
"""
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert app.started == {"group-flaky": "FAILED"}
        states = {state["id"]: state for state in app.row_states()}
        assert states["group-flaky"]["state"] == "failed"
        assert states["flaky-child"]["state"] == "failed"
        assert [failure.id for failure in app.failures] == ["flaky-child"]
    finally:
        await app.stop()


async def test_patch_layer_can_disable_group(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "group-patched"

[[plugins.rows]]
id = "patched-child"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "patched" }
"""
    composition = _composition(tmp_path, body)
    manifest = _write(
        tmp_path,
        "dpyro.toml",
        """[[plugins]]
id = "group-patched"
disabled = true
""",
    )
    composition.apply(rows_from_specs(load_manifest(str(manifest)), origin="manifest:dpyro.toml"))
    _reset_order()
    app = await _boot_composition(composition)
    try:
        assert plugins_dummy.MOUNT_ORDER == []
        states = {state["id"]: state for state in app.row_states()}
        assert states["group-patched"]["state"] == "disabled"
        assert states["patched-child"]["state"] == "disabled"
        assert states["group-patched"]["overridden"] is True
    finally:
        await app.stop()


async def test_include_toml_splices_rows_in_place(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "extra.toml",
        """[[plugins]]
id = "inc-a"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "inc-a" }

[[plugins]]
id = "inc-b"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "inc-b" }
""",
    )
    body = (
        _ordered_row("first-row", "first")
        + """[[plugins]]
id = "include-rows"
include = "extra.toml"
"""
        + _ordered_row("last-row", "last")
    )
    composition = _composition(tmp_path, body)
    assert [row.id for row in composition.rows()] == ["first-row", "inc-a", "inc-b", "last-row"]
    _reset_order()
    app = await _boot_composition(composition)
    try:
        assert plugins_dummy.MOUNT_ORDER == ["first", "inc-a", "inc-b", "last"]
        assert app.started == {
            "first-row": "ACTIVE",
            "inc-a": "ACTIVE",
            "inc-b": "ACTIVE",
            "last-row": "ACTIVE",
        }
    finally:
        await app.stop()


async def test_include_json_splices_rows_in_place(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "extra.json",
        '[{"id": "json-a", "plugin": "tests.kernel.plugins_dummy:OrderedPlugin", "config": {"tag": "json-a"}}]',
    )
    body = (
        _ordered_row("first-row", "first")
        + """[[plugins]]
id = "include-json"
include = "extra.json"
"""
        + _ordered_row("last-row", "last")
    )
    _reset_order()
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert plugins_dummy.MOUNT_ORDER == ["first", "json-a", "last"]
        assert [row.id for row in app.rows] == ["first-row", "json-a", "last-row"]
    finally:
        await app.stop()


async def test_group_include_appends_children(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "children.toml",
        """[[plugins]]
id = "inc-child"
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
config = { tag = "inc-child" }
""",
    )
    body = """[[plugins]]
id = "group-with-include"
group = true
include = "children.toml"
"""
    _reset_order()
    app = await _boot_composition(_composition(tmp_path, body))
    try:
        assert [row.id for row in app.rows] == ["group-with-include"]
        states = {state["id"]: state for state in app.row_states()}
        assert states["group-with-include"]["children"] == ["inc-child"]
        assert states["inc-child"]["parent"] == "group-with-include"
        assert plugins_dummy.MOUNT_ORDER == ["inc-child"]
    finally:
        await app.stop()


def test_include_cycle_raises_composition_error(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "loop.toml",
        """[[plugins]]
id = "loop"
include = "loop.toml"
""",
    )
    path = _write(
        tmp_path,
        "bundle.toml",
        _bundle_text(
            """[[plugins]]
id = "loop-entry"
include = "loop.toml"
"""
        ),
    )
    with pytest.raises(CompositionError, match="include cycle"):
        load_bundle(path)


def test_include_unknown_format_raises_composition_error(tmp_path: Path) -> None:
    _write(tmp_path, "extra.yaml", "plugins: []\n")
    path = _write(
        tmp_path,
        "bundle.toml",
        _bundle_text(
            """[[plugins]]
id = "yaml-include"
include = "extra.yaml"
"""
        ),
    )
    with pytest.raises(CompositionError, match="unsupported include format"):
        load_bundle(path)


def test_include_missing_file_raises_composition_error(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "bundle.toml",
        _bundle_text(
            """[[plugins]]
id = "missing-include"
include = "nope.toml"
"""
        ),
    )
    with pytest.raises(CompositionError, match="include file not found"):
        load_bundle(path)


def test_container_row_must_not_declare_plugin(tmp_path: Path) -> None:
    body = """[[plugins]]
id = "bad-container"
group = true
plugin = "tests.kernel.plugins_dummy:OrderedPlugin"
"""
    with pytest.raises(CompositionError, match="must not declare a plugin target"):
        _composition(tmp_path, body)
