"""Behaviour snapshots for the plugin install public API.

The install module is being split into private helper modules. These tests pin
only the observable behaviour of the public entry points: the returned report,
the files written below the plugin root, the user-manifest bytes and the
rewritten ``plugin.toml``. They use temporary directories and a fake uv runner,
so they never reach the network or the real user home.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from deeppyro.plugins import install
from deeppyro.plugins.bootstrap import scan_plugin_packages
from deeppyro.plugins.install import (
    InstallFailure,
    PreflightFailure,
    UnknownPlugin,
    UvCommand,
    UvResult,
    vendor_versions,
)
from deeppyro.plugins.metadata import check_compat, dpyro_version, load_metadata, python_tag

DEMO_MANIFEST = '[[plugins]]\nid = "demo"\nname = "demo_plugin:DemoPlugin"\n'


@pytest.fixture(autouse=True)
def isolated_plugin_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point every user-level path at a temporary directory for one test."""
    plugins = tmp_path / "plugins"
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(plugins))
    monkeypatch.setenv("DEEPPYRO_PLUGINS_MANIFEST", str(tmp_path / "plugins.toml"))
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    return SimpleNamespace(tmp=tmp_path, root=plugins, manifest=tmp_path / "plugins.toml")


def plugin_toml(
    plugin_id: str,
    *,
    version: str = "1.0.0",
    requires: tuple[str, ...] = (),
    exempt: tuple[str, ...] | None = None,
) -> str:
    module = f"{plugin_id}_plugin"
    lines = [
        "[package]",
        f'id = "{plugin_id}"',
        'name = "Demo Plugin"',
        f'version = "{version}"',
        'description = "fixture"',
        f'module = "{module}"',
        'class = "DemoPlugin"',
        "api_version = 1",
        'provides = ["demo_service"]',
    ]
    if exempt is not None:
        lines += ["", "[compat]", 'dpyro = ">=99"', f"dpyro_exempt_versions = {json.dumps(list(exempt))}"]
    if requires:
        rendered = ", ".join(f'"{requirement}"' for requirement in requires)
        lines += ["", "[deps]", f"requires = [{rendered}]"]
    return "\n".join(lines) + "\n"


def make_package(
    parent: Path,
    plugin_id: str = "demo",
    *,
    version: str = "1.0.0",
    requires: tuple[str, ...] = (),
    exempt: tuple[str, ...] | None = None,
) -> Path:
    directory = parent / plugin_id
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(
        plugin_toml(plugin_id, version=version, requires=requires, exempt=exempt),
        encoding="utf-8",
    )
    (directory / f"{plugin_id}_plugin.py").write_text(
        'from deeppyro.api import Plugin\n\n\nclass DemoPlugin(Plugin):\n    provide = "demo_service"\n',
        encoding="utf-8",
    )
    return directory


def write_dist(directory: Path, name: str, version: str) -> Path:
    info = directory / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    return info


class FakeRunner:
    """Stands in for uv: records commands and materialises the requested files."""

    def __init__(self, *, vendor: dict[str, str] | None = None, fail_at: str | None = None) -> None:
        self.vendor = vendor or {}
        self.fail_at = fail_at
        self.commands: list[UvCommand] = []

    def __call__(self, command: UvCommand) -> UvResult:
        self.commands.append(command)
        joined = " ".join(command.args)
        if self.fail_at is not None and self.fail_at in joined:
            return UvResult(1, "", "uv exploded")
        if "install" in command.args:
            target = Path(command.args[command.args.index("--target") + 1])
            target.mkdir(parents=True, exist_ok=True)
            for name, version in self.vendor.items():
                write_dist(target, name, version)
                (target / f"{name.replace('-', '_')}.py").write_text("", encoding="utf-8")
        return UvResult(0, "", "")


# -- add -------------------------------------------------------------------


def test_snapshot_add_local_package(isolated_plugin_paths: SimpleNamespace) -> None:
    workspace = isolated_plugin_paths
    package = make_package(workspace.tmp / "src")
    source_toml = (package / "plugin.toml").read_text(encoding="utf-8")

    report = install.add(package)

    target = workspace.root / "demo"
    assert report == install.InstallReport(
        plugin_id="demo",
        version="1.0.0",
        directory=target,
        replaced=False,
        manifest_changed=True,
        vendor={},
        warnings=(),
    )
    assert workspace.manifest.read_text(encoding="utf-8") == DEMO_MANIFEST
    installed = sorted(path.name for path in target.iterdir() if path.name != "__pycache__")
    assert installed == ["demo_plugin.py", "plugin.toml"]
    assert (target / "plugin.toml").read_text(encoding="utf-8") == source_toml
    assert not list(workspace.root.glob(".dpyro-*"))
    assert [(row.id, row.version) for row in scan_plugin_packages(workspace.root)] == [("demo", "1.0.0")]

    with pytest.raises(PreflightFailure) as blocked:
        install.add(package)
    assert blocked.value.problems == (f"{target} already exists (use --force to replace it)",)

    replaced = install.add(package, force=True)
    assert replaced == install.InstallReport(
        plugin_id="demo",
        version="1.0.0",
        directory=target,
        replaced=True,
        manifest_changed=False,
        vendor={},
        warnings=(),
    )
    assert workspace.manifest.read_text(encoding="utf-8") == DEMO_MANIFEST


def test_snapshot_add_dependencies_with_fake_uv(isolated_plugin_paths: SimpleNamespace) -> None:
    workspace = isolated_plugin_paths
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    runner = FakeRunner(vendor={"websockets": "15.1"})

    report = install.add(package, runner=runner)

    vendor = workspace.root / "demo" / "_vendor" / python_tag()
    assert report == install.InstallReport(
        plugin_id="demo",
        version="1.0.0",
        directory=workspace.root / "demo",
        replaced=False,
        manifest_changed=True,
        vendor={"websockets": "15.1"},
        warnings=(),
    )
    assert vendor_versions(vendor) == {"websockets": "15.1"}
    assert sorted(path.name for path in vendor.iterdir()) == [
        "requirements.txt",
        "websockets-15.1.dist-info",
        "websockets.py",
    ]
    assert (vendor / "requirements.txt").read_text(encoding="utf-8") == "websockets>=15\n"
    (command,) = runner.commands
    assert command.args[:5] == ("pip", "install", "--python", sys.executable, "--target")
    assert command.args[5].endswith(str(Path("_vendor") / python_tag()))
    assert Path(command.args[5]).name == python_tag()
    assert Path(command.args[7]).parent == Path(command.args[5])
    assert Path(command.args[7]).name == "requirements.txt"
    assert command.args[6] == "-r"
    assert command.cwd is None


def test_snapshot_add_no_deps_warning(isolated_plugin_paths: SimpleNamespace) -> None:
    workspace = isolated_plugin_paths
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))

    report = install.add(package, no_deps=True)

    assert report.vendor == {}
    assert report.warnings == ("declared dependencies were not installed (--no-deps)",)
    assert not (workspace.root / "demo" / "_vendor").exists()


# -- remove ----------------------------------------------------------------


def test_snapshot_remove_dependents_and_failure_paths(isolated_plugin_paths: SimpleNamespace) -> None:
    workspace = isolated_plugin_paths
    install.add(make_package(workspace.tmp / "src", "base"))
    install.add(make_package(workspace.tmp / "src", "dependent", requires=("base>=1",)), no_deps=True)
    manifest_before = workspace.manifest.read_text(encoding="utf-8")

    with pytest.raises(PreflightFailure) as blocked:
        install.remove("base")

    assert blocked.value.problems == ("dependent depends on base",)
    assert (workspace.root / "base" / "plugin.toml").is_file()
    assert workspace.manifest.read_text(encoding="utf-8") == manifest_before

    report = install.remove("base", force=True)

    assert report == install.RemoveReport(plugin_id="base", dependents=("dependent",), manifest_changed=True)
    assert not (workspace.root / "base").exists()
    assert workspace.manifest.read_text(encoding="utf-8") == (
        '[[plugins]]\nid = "dependent"\nname = "dependent_plugin:DemoPlugin"\n'
    )
    with pytest.raises(UnknownPlugin):
        install.remove("missing")


# -- update ----------------------------------------------------------------


def test_snapshot_update_reports_vendor_diff(isolated_plugin_paths: SimpleNamespace) -> None:
    workspace = isolated_plugin_paths
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    install.add(package, no_deps=True)
    write_dist(workspace.root / "demo" / "_vendor" / python_tag(), "websockets", "15.0")
    runner = FakeRunner(vendor={"websockets": "15.1"})

    report = install.update("demo", runner=runner)

    assert report == install.UpdateReport(
        plugin_id="demo",
        rebuilt=True,
        changes=(install.VendorChange(name="websockets", before="15.0", after="15.1"),),
        warnings=(),
    )
    vendor = workspace.root / "demo" / "_vendor" / python_tag()
    assert vendor_versions(vendor) == {"websockets": "15.1"}
    assert [command.args[0] for command in runner.commands] == ["pip"]
    assert not list(workspace.root.glob(".dpyro-update-*"))


def test_snapshot_update_lock_failure_restores_uv_lock(isolated_plugin_paths: SimpleNamespace) -> None:
    workspace = isolated_plugin_paths
    package = make_package(workspace.tmp / "src")
    (package / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '1.0.0'\n", encoding="utf-8")
    (package / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    install.add(package)
    lock = workspace.root / "demo" / "uv.lock"
    original = lock.read_bytes()
    runner = FakeRunner(fail_at="export")

    with pytest.raises(InstallFailure) as failure:
        install.update("demo", runner=runner)

    message = str(failure.value)
    assert failure.value.rolled_back is True
    assert failure.value.stderr_tail == "uv exploded"
    prefix = f"cd {workspace.root / 'demo'} && uv export --format requirements-txt --no-hashes --output-file "
    assert message.startswith(prefix)
    assert message.endswith(" failed (exit 1): uv exploded")
    assert lock.read_bytes() == original
    assert not list(workspace.root.glob(".dpyro-update-*"))


# -- allow-version ---------------------------------------------------------


def test_snapshot_allow_version_rewrites_plugin_toml(isolated_plugin_paths: SimpleNamespace) -> None:
    workspace = isolated_plugin_paths
    directory = workspace.root / "demo"
    directory.mkdir(parents=True)
    original = plugin_toml("demo", exempt=())
    toml_path = directory / "plugin.toml"
    toml_path.write_text(original, encoding="utf-8")
    version = dpyro_version()
    expected = original.replace(
        "dpyro_exempt_versions = []",
        f'dpyro_exempt_versions = ["{version}"]',
    )

    report = install.allow_version("demo", version)

    assert report == install.AllowVersionReport(plugin_id="demo", version=version, changed=True)
    assert toml_path.read_text(encoding="utf-8") == expected
    assert (directory / "plugin.toml.bak").read_text(encoding="utf-8") == original
    assert check_compat(load_metadata(toml_path, plugin_id="demo")) == []

    again = install.allow_version("demo", version)
    assert again == install.AllowVersionReport(plugin_id="demo", version=version, changed=False)
    assert toml_path.read_text(encoding="utf-8") == expected
    assert (directory / "plugin.toml.bak").read_text(encoding="utf-8") == original
