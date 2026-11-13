"""Tests for [bundle].requires dependency ordering and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.kernel import composition as composition_module
from deeppyro.kernel.composition import (
    BundleRequirement,
    CompositionError,
    default_composition,
    load_bundle,
)

DUMMY = "tests.kernel.plugins_dummy"


def _bundle_toml(
    bundle_id: str,
    *,
    version: str = "0.1.0",
    order: int = 0,
    requires: str = "",
    rows: tuple[str, ...] = (),
) -> str:
    lines = [
        "[bundle]",
        f"id = {bundle_id!r}",
        'name = "External bundle"',
        f"version = {version!r}",
        f"order = {order}",
        "critical = false",
    ]
    if requires:
        lines.append(requires)
    for row_id in rows:
        lines.extend(
            [
                "",
                "[[plugins]]",
                f"id = {row_id!r}",
                f"plugin = {DUMMY + ':OrderedPlugin'!r}",
                f"config = {{ tag = {row_id!r} }}",
            ]
        )
    return "\n".join(lines) + "\n"


def _bundle(tmp_path: Path, dirname: str, text: str) -> Path:
    directory = tmp_path / dirname
    directory.mkdir()
    (directory / "bundle.toml").write_text(text, encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def _no_user_bundles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(composition_module, "discover_user_bundles", lambda root=None: [])


def test_requires_accepts_string_and_table_forms(tmp_path: Path) -> None:
    string_bundle = _bundle(
        tmp_path,
        "string-form",
        _bundle_toml("string-form", requires='requires = ["core>=0.1", "alpha==0.2.0"]'),
    )
    table_bundle = _bundle(
        tmp_path,
        "table-form",
        _bundle_toml(
            "table-form",
            requires='[[bundle.requires]]\nid = "core"\nversion = ">=0.1"\n'
            '\n[[bundle.requires]]\nid = "alpha"\n',
        ),
    )
    string_requires = load_bundle(string_bundle).requires
    table_requires = load_bundle(table_bundle).requires
    assert string_requires == (
        BundleRequirement(id="core", version=">=0.1"),
        BundleRequirement(id="alpha", version="==0.2.0"),
    )
    assert table_requires == (
        BundleRequirement(id="core", version=">=0.1"),
        BundleRequirement(id="alpha", version=None),
    )


def test_unsupported_version_constraint_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, "caret", _bundle_toml("caret", requires='requires = ["core^0.1"]'))
    with pytest.raises(CompositionError, match="unsupported version constraint"):
        load_bundle(bundle)


def test_default_composition_orders_dependencies_first(tmp_path: Path) -> None:
    dependency = _bundle(tmp_path, "alpha", _bundle_toml("alpha", order=5, rows=("alpha-row",)))
    dependent = _bundle(
        tmp_path,
        "beta",
        _bundle_toml("beta", order=1, requires='requires = ["alpha>=0.1"]', rows=("beta-row",)),
    )
    composition = default_composition(bundle_paths=[dependent, dependency])
    ids = [row.id for row in composition.rows()]
    assert ids[0] == "kernel"
    assert ids.index("alpha-row") < ids.index("beta-row")


def test_default_composition_accepts_core_requirement(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        "needs-core",
        _bundle_toml("needs-core", requires='requires = ["core>=0.1"]', rows=("needs-core-row",)),
    )
    composition = default_composition(bundle_paths=[bundle])
    assert "needs-core-row" in [row.id for row in composition.rows()]


def test_missing_required_bundle_raises(tmp_path: Path) -> None:
    bundle = _bundle(
        tmp_path,
        "orphan",
        _bundle_toml("orphan", requires='requires = ["missing>=0.1"]'),
    )
    with pytest.raises(CompositionError, match="requires missing bundle"):
        default_composition(bundle_paths=[bundle])


def test_version_conflict_raises(tmp_path: Path) -> None:
    dependency = _bundle(tmp_path, "alpha", _bundle_toml("alpha", version="0.1.0"))
    dependent = _bundle(
        tmp_path,
        "beta",
        _bundle_toml("beta", requires='requires = ["alpha>=0.2"]'),
    )
    with pytest.raises(CompositionError, match="but version 0.1.0 is installed"):
        default_composition(bundle_paths=[dependency, dependent])


def test_bundle_dependency_cycle_raises(tmp_path: Path) -> None:
    alpha = _bundle(tmp_path, "alpha", _bundle_toml("alpha", requires='requires = ["beta"]'))
    beta = _bundle(tmp_path, "beta", _bundle_toml("beta", requires='requires = ["alpha"]'))
    with pytest.raises(CompositionError, match="bundle dependency cycle"):
        default_composition(bundle_paths=[alpha, beta])
