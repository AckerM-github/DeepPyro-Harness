"""The frozen build must carry the whole shipped composition.

``src/deeppyro/bundles/core/bundle.toml`` names its plugin modules as strings, so
no static analyser can see them: a bare PyInstaller run drops every plugin that
is not imported by the entry point, together with the third-party libraries only
they use (httpx, Pillow).  ``packaging/hiddenimports.py`` derives the
frozen inputs from that data; these gates keep the derivation honest as bundle
rows, dynamic imports and data files change.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "packaging" / "hiddenimports.py"
CORE_BUNDLE = ROOT / "src" / "deeppyro" / "bundles" / "core" / "bundle.toml"


def _generator() -> ModuleType:
    """Load the build-input generator by path: it must not import the harness."""
    spec = importlib.util.spec_from_file_location("_packaging_hiddenimports", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_ships_the_files_the_spec_passes_to_pyinstaller() -> None:
    module = _generator()
    assert module.CORE_BUNDLE == CORE_BUNDLE
    assert module.LAUNCHER.is_file()
    assert module.SRC_ROOT == ROOT / "src"


def test_every_core_bundle_row_generates_a_hidden_import(tmp_path: Path) -> None:
    module = _generator()
    targets = module.bundle_targets(CORE_BUNDLE)
    assert targets, "the core bundle declares no plugin rows"
    assert [target for target in targets if module.module_name(target) is None] == []

    generated = module.collect_hiddenimports(bundles_dir=tmp_path, plugins_dir=tmp_path)
    missing = [name for name in module.core_bundle_modules() if name not in generated]
    assert missing == []
    assert set(module.DYNAMIC_HIDDENIMPORTS) <= set(generated)


def test_generated_list_is_sorted_and_free_of_duplicates(tmp_path: Path) -> None:
    generated = _generator().collect_hiddenimports(bundles_dir=tmp_path, plugins_dir=tmp_path)
    assert generated == sorted(set(generated))
    assert len(generated) == len(set(generated))


def test_data_entries_point_at_real_paths() -> None:
    module = _generator()
    entries = module.data_files()
    assert entries
    assert [source for source, _dest in entries if not Path(source).exists()] == []
    destinations = [dest for _source, dest in entries]
    assert "bundles/core" in destinations
    # Destinations are relative to sys._MEIPASS, never absolute paths.
    assert all(not Path(dest).is_absolute() for dest in destinations)


def test_path_like_targets_never_become_hidden_imports() -> None:
    module = _generator()
    assert module.module_name("deeppyro.builtin.llm:LlmPlugin") == "deeppyro.builtin.llm"
    assert module.module_name("./plugin.py:Thing") is None
    assert module.module_name("plugins/demo.py:Thing") is None
    assert module.module_name("demo") is None


def test_plugin_packages_are_frozen_only_on_request(tmp_path: Path) -> None:
    module = _generator()
    plugin = tmp_path / "demo"
    plugin.mkdir()
    (plugin / "plugin.toml").write_text('module = "demo.plugin"\nclass = "DemoPlugin"\n', encoding="utf-8")

    without = module.collect_hiddenimports(bundles_dir=tmp_path, plugins_dir=tmp_path)
    with_plugins = module.collect_hiddenimports(
        bundles_dir=tmp_path, plugins_dir=tmp_path, include_plugins=True
    )

    assert "demo.plugin" not in without
    assert "demo.plugin" in with_plugins
