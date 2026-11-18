"""Tests for patching one row inside a group table from a patch layer.

A bundle owns the shape of a group; patch layers may address one child by its
qualified address (parent/child) and change its config/disabled/critical
fields.  The declaring bundle.toml is never rewritten.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from deeppyro.kernel.composition import (
    Composition,
    CompositionError,
    PluginRow,
    load_bundle,
    rows_from_specs,
)
from deeppyro.kernel.manifest import PluginSpec

ORDERED = "tests.kernel.plugins_dummy:OrderedPlugin"
COUNTER = "tests.kernel.plugins_dummy:CounterPlugin"

BUNDLE_HEADER = """[bundle]
id = "test"
name = "Group patch"
version = "0.1.0"

"""

GROUP_BODY = (
    """[[plugins]]
id = "solo"
plugin = \""""
    + ORDERED
    + """\"
config = { tag = "solo" }

# comment that belongs to the group declaration
[[plugins]]
id = "group-a"

[[plugins.rows]]
id = "child-a"
plugin = \""""
    + ORDERED
    + """\"
config = { tag = "a" }

[[plugins.rows]]
id = "child-b"
plugin = \""""
    + ORDERED
    + """\"
config = { tag = "b" }
"""
)

NESTED_BODY = (
    """[[plugins]]
id = "outer"

[[plugins.rows]]
id = "inner"

[[plugins.rows.rows]]
id = "deep"
plugin = \""""
    + ORDERED
    + """\"
config = { tag = "deep" }
"""
)

TWO_GROUPS_BODY = (
    """[[plugins]]
id = "group-a"

[[plugins.rows]]
id = "shared"
plugin = \""""
    + ORDERED
    + """\"
config = { tag = "left" }

[[plugins]]
id = "group-b"

[[plugins.rows]]
id = "shared"
plugin = \""""
    + ORDERED
    + """\"
config = { tag = "right" }
"""
)


def _write_bundle(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bundle.toml"
    path.write_bytes((BUNDLE_HEADER + body).encode("utf-8"))
    return path


def _composition(tmp_path: Path, body: str) -> tuple[Composition, Path]:
    path = _write_bundle(tmp_path, body)
    return Composition(load_bundle(path).rows, origin="test"), path


def _patch(specs: list[PluginSpec]) -> list[PluginRow]:
    return rows_from_specs(specs, origin="manifest:test-patch")


def _child(composition: Composition, address: str) -> PluginRow:
    row = composition.get(address)
    assert row is not None, address
    return row


def test_dotted_patch_updates_only_the_addressed_child(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    composition.apply(
        _patch([PluginSpec(id="group-a/child-b", config={"tag": "patched"})]),
        origin="manifest:test-patch",
    )

    patched = _child(composition, "group-a/child-b")
    untouched = _child(composition, "group-a/child-a")
    group = _child(composition, "group-a")
    assert patched.config == {"tag": "patched"}
    assert untouched.config == {"tag": "a"}
    assert patched.id == "child-b"
    assert "manifest:test-patch" in patched.origin
    assert patched.overridden is True
    assert group.config is None
    assert group.overridden is False
    assert patched.critical is None


def test_child_patch_does_not_rewrite_the_bundle_file(tmp_path: Path) -> None:
    composition, path = _composition(tmp_path, GROUP_BODY)
    before = path.read_bytes()
    assert b"# comment that belongs to the group declaration" in before

    composition.apply(
        _patch([PluginSpec(id="group-a/child-a", config={"tag": "patched"})]),
        origin="manifest:test-patch",
    )

    assert path.read_bytes() == before


def test_child_patch_can_disable_and_mark_critical(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    composition.apply(
        _patch([PluginSpec(id="group-a/child-a", disabled=True)]),
        origin="manifest:test-patch",
    )
    composition.apply(
        [PluginRow(id="group-a/child-a", critical=False, origin="manifest:test-patch")],
        origin="manifest:test-patch",
    )

    child = _child(composition, "group-a/child-a")
    assert child.disabled is True
    assert child.critical is False
    # The group row itself stays enabled so the other child keeps mounting.
    group = _child(composition, "group-a")
    assert group.disabled is None


def test_child_patch_keeps_group_structure_and_order(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)
    order_before = list(composition.order)

    composition.apply(
        _patch([PluginSpec(id="group-a/child-b", config={"tag": "patched"})]),
        origin="manifest:test-patch",
    )

    assert list(composition.order) == order_before
    group = _child(composition, "group-a")
    assert [child.id for child in group.children] == ["child-a", "child-b"]
    assert [row.id for row in composition.flat()] == [
        "solo",
        "group-a",
        "child-a",
        "child-b",
    ]


def test_find_resolves_top_level_dotted_and_unique_plain_ids(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    top = composition.find("group-a")
    dotted = composition.find("group-a/child-b")
    plain = composition.find("child-b")

    assert top is not None and top[0] == "group-a"
    assert top[1] is _child(composition, "group-a")
    assert dotted is not None and dotted[0] == "group-a/child-b"
    assert dotted[1] is _child(composition, "group-a/child-b")
    assert plain is not None and plain[0] == "group-a/child-b"
    assert plain[1] is _child(composition, "group-a/child-b")
    assert composition.find("ghost") is None
    assert composition.find("group-a/ghost") is None


def test_find_returns_none_for_ambiguous_plain_child_ids(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, TWO_GROUPS_BODY)

    assert composition.find("shared") is None
    left = composition.find("group-a/shared")
    right = composition.find("group-b/shared")
    assert left is not None and left[0] == "group-a/shared"
    assert right is not None and right[0] == "group-b/shared"


def test_nested_group_child_addresses_resolve(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, NESTED_BODY)

    composition.apply(
        _patch([PluginSpec(id="outer/inner/deep", config={"tag": "patched"})]),
        origin="manifest:test-patch",
    )

    assert _child(composition, "outer/inner/deep").config == {"tag": "patched"}
    found = composition.find("deep")
    assert found is not None and found[0] == "outer/inner/deep"
    # The intermediate group stays a container row.
    assert _child(composition, "outer/inner").group is True


def test_child_patch_rejects_unknown_child_address(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    with pytest.raises(CompositionError, match="has no child 'nope'"):
        composition.apply(
            _patch([PluginSpec(id="group-a/nope", config={"tag": "x"})]),
            origin="manifest:test-patch",
        )


def test_child_patch_rejects_non_group_parent(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    with pytest.raises(CompositionError, match="is not a group"):
        composition.apply(
            _patch([PluginSpec(id="solo/child", config={"tag": "x"})]),
            origin="manifest:test-patch",
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"group": True}, "cannot become a group"),
        (
            {"children": (PluginRow(id="extra", plugin=ORDERED),)},
            "cannot add or replace child rows",
        ),
        ({"before": "child-a"}, "cannot be reordered"),
        ({"after": "child-a"}, "cannot be reordered"),
    ],
)
def test_child_patch_rejects_structural_changes(
    tmp_path: Path,
    changes: dict[str, object],
    message: str,
) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)
    row = replace(
        PluginRow(id="group-a/child-b", origin="manifest:test-patch"),
        **changes,  # type: ignore[arg-type]
    )

    with pytest.raises(CompositionError, match=message):
        composition.apply([row], origin="manifest:test-patch")


def test_child_patch_rejects_class_change(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    with pytest.raises(CompositionError, match="cannot change class"):
        composition.apply(
            _patch([PluginSpec(id="group-a/child-a", name=COUNTER)]),
            origin="manifest:test-patch",
        )


def test_child_patch_accepts_the_same_plugin_class(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    composition.apply(
        _patch([PluginSpec(id="group-a/child-a", name=ORDERED, config={"tag": "same-class"})]),
        origin="manifest:test-patch",
    )

    assert _child(composition, "group-a/child-a").config == {"tag": "same-class"}


def test_unknown_plain_patch_id_still_requires_a_plugin_target(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)

    with pytest.raises(CompositionError, match="unknown plugin id 'ghost'"):
        composition.apply(
            _patch([PluginSpec(id="ghost")]),
            origin="manifest:test-patch",
        )


def test_applying_the_same_child_patch_twice_is_idempotent(tmp_path: Path) -> None:
    composition, _path = _composition(tmp_path, GROUP_BODY)
    patch = _patch([PluginSpec(id="group-a/child-b", config={"tag": "patched"})])

    composition.apply(patch, origin="manifest:test-patch")
    first = composition.rows()
    composition.apply(patch, origin="manifest:test-patch")

    assert composition.rows() == first
