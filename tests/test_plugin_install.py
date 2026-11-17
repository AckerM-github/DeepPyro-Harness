"""Plugin lifecycle verbs: preflight, atomic install, rollback and conflicts."""

from __future__ import annotations

import json
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from deeppyro.plugins import cli, install
from deeppyro.plugins.bootstrap import scan_plugin_packages
from deeppyro.plugins.install import (
    InstallFailure,
    PreflightFailure,
    UnknownPlugin,
    UvCommand,
    UvResult,
    vendor_versions,
    with_exempt_version,
)
from deeppyro.plugins.metadata import (
    FAILURE_DEP_CONFLICT,
    FAILURE_DEP_MISSING,
    check_compat,
    dpyro_version,
    load_metadata,
    python_tag,
)


def plugin_toml(
    plugin_id: str,
    *,
    version: str = "1.0.0",
    module: str | None = None,
    requires: tuple[str, ...] = (),
    dpyro: str = "",
) -> str:
    module = module or f"{plugin_id}_plugin"
    lines = [
        "[package]",
        f'id = "{plugin_id}"',
        'name = "Demo Plugin"',
        f'version = "{version}"',
        'description = "fixture"',
        f'module = "{module}"',
        'class = "DemoPlugin"',
        "api_version = 3",
        'provides = ["demo_service"]',
    ]
    if dpyro:
        lines += ["", "[compat]", f'dpyro = "{dpyro}"']
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
    dpyro: str = "",
    module: str | None = None,
) -> Path:
    directory = parent / plugin_id
    directory.mkdir(parents=True)
    module_name = module or f"{plugin_id}_plugin"
    text = plugin_toml(
        plugin_id,
        version=version,
        module=module_name,
        requires=requires,
        dpyro=dpyro,
    )
    (directory / "plugin.toml").write_text(text, encoding="utf-8")
    (directory / f"{module_name}.py").write_text(
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
    """Stands in for uv: records commands and materialises payloads."""

    def __init__(
        self,
        *,
        payload: Path | None = None,
        vendor: dict[str, str] | None = None,
        fail_at: str | None = None,
        stderr: str = "uv exploded",
    ) -> None:
        self.payload = payload
        self.vendor = vendor or {}
        self.fail_at = fail_at
        self.stderr = stderr
        self.commands: list[UvCommand] = []

    def __call__(self, command: UvCommand) -> UvResult:
        self.commands.append(command)
        joined = " ".join(command.args)
        if self.fail_at and self.fail_at in joined:
            return UvResult(1, "", self.stderr)
        if "install" in command.args:
            target = Path(command.args[command.args.index("--target") + 1])
            if "--no-deps" in command.args:
                if self.payload is not None:
                    shutil.copytree(self.payload, target, dirs_exist_ok=True)
            else:
                target.mkdir(parents=True, exist_ok=True)
                for name, version in self.vendor.items():
                    write_dist(target, name, version)
                    (target / f"{name.replace('-', '_')}.py").write_text("", encoding="utf-8")
            return UvResult(0, "", "")
        if command.args and command.args[0] == "export":
            output = Path(command.args[command.args.index("--output-file") + 1])
            output.write_text("websockets==15.1\n", encoding="utf-8")
        return UvResult(0, "", "")


def zip_archive(source: Path, destination: Path, *, top_level: bool = False) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w") as archive:
        for path in sorted(source.rglob("*")):
            if not path.is_file():
                continue
            arcname = path.relative_to(source)
            if top_level:
                arcname = Path(source.name) / arcname
            archive.write(path, arcname.as_posix())
    return destination


def tar_archive(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(source, arcname=source.name)
    return destination


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    plugins = tmp_path / "plugins"
    manifest = tmp_path / "plugins.toml"
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(plugins))
    monkeypatch.setenv("DEEPPYRO_PLUGINS_MANIFEST", str(manifest))
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    modules_before = set(sys.modules)
    path_before = list(sys.path)
    yield SimpleNamespace(tmp=tmp_path, plugins=plugins, manifest=manifest)
    sys.path[:] = path_before
    for name in set(sys.modules) - modules_before:
        sys.modules.pop(name, None)


def staging_directories(workspace: SimpleNamespace) -> list[Path]:
    if not workspace.plugins.is_dir():
        return []
    return list(workspace.plugins.glob(".dpyro-*"))


# -- add: local sources ----------------------------------------------------


def test_add_local_directory_records_manifest(workspace, capsys) -> None:
    package = make_package(workspace.tmp / "src")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "stale.pyc").write_bytes(b"stale")

    assert cli.main(["add", str(package)]) == cli.EXIT_RESTART

    target = workspace.plugins / "demo"
    assert (target / "plugin.toml").is_file()
    assert (target / "demo_plugin.py").is_file()
    assert not (target / "__pycache__" / "stale.pyc").exists()
    manifest = workspace.manifest.read_text(encoding="utf-8")
    assert 'id = "demo"' in manifest
    assert "demo_plugin:DemoPlugin" in manifest
    assert "restart deeppyro" in capsys.readouterr().out
    assert staging_directories(workspace) == []


def test_add_force_reinstall_reports_no_manifest_change(workspace, capsys) -> None:
    package = make_package(workspace.tmp / "src")
    assert install.add(package).manifest_changed is True

    assert cli.main(["add", str(package), "--force"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "replaced demo 1.0.0" in out
    assert "restart deeppyro" not in out
    assert not list(workspace.tmp.glob(".dpyro-backup-*"))


def test_add_existing_target_requires_force(workspace) -> None:
    package = make_package(workspace.tmp / "src")
    install.add(package)

    with pytest.raises(PreflightFailure) as info:
        install.add(package)

    assert any("already exists" in problem for problem in info.value.problems)
    assert scan_plugin_packages(workspace.plugins)[0].version == "1.0.0"


def test_add_force_replaces_existing_files(workspace) -> None:
    package = make_package(workspace.tmp / "src")
    install.add(package)
    stale = workspace.plugins / "demo" / "stale.txt"
    stale.write_text("old", encoding="utf-8")

    report = install.add(package, force=True)

    assert report.replaced is True
    assert not stale.exists()


def test_add_bad_metadata_installs_nothing(workspace, capsys) -> None:
    package = workspace.tmp / "broken"
    package.mkdir()
    (package / "plugin.toml").write_text(
        '[package]\nid = "broken"\nname = "Broken"\nmodule = "broken_plugin"\nclass = "C"\n', encoding="utf-8"
    )

    assert cli.main(["add", str(package)]) == cli.EXIT_PREFLIGHT

    err = capsys.readouterr().err
    assert "preflight failed; nothing was installed" in err
    assert "api_version" in err
    assert not (workspace.plugins / "broken").exists()
    assert staging_directories(workspace) == []
    assert not workspace.manifest.exists()


def test_add_incompatible_compat_is_preflight(workspace) -> None:
    package = make_package(workspace.tmp / "src", dpyro=">=99")

    with pytest.raises(PreflightFailure) as info:
        install.add(package)

    assert any("version-incompat" in problem for problem in info.value.problems)
    assert not (workspace.plugins / "demo").exists()


def test_add_missing_source_is_preflight(workspace) -> None:
    with pytest.raises(PreflightFailure):
        install.add(workspace.tmp / "src" / "nope")


# -- add: uv driven installs -----------------------------------------------


def test_add_dependency_failure_rolls_back(workspace) -> None:
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    runner = FakeRunner(fail_at="-r")

    with pytest.raises(InstallFailure) as info:
        install.add(package, runner=runner)

    assert info.value.rolled_back is True
    assert "uv exploded" in info.value.stderr_tail
    assert not (workspace.plugins / "demo").exists()
    assert not workspace.manifest.exists()
    assert staging_directories(workspace) == []


def test_add_installs_requires_into_python_tagged_vendor(workspace) -> None:
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    runner = FakeRunner(vendor={"websockets": "15.0.1"})

    report = install.add(package, runner=runner, offline=True)

    vendor = workspace.plugins / "demo" / "_vendor" / python_tag()
    assert vendor.is_dir()
    assert report.vendor == {"websockets": "15.0.1"}
    assert vendor_versions(vendor) == {"websockets": "15.0.1"}
    assert all("--offline" in command.args for command in runner.commands)
    assert (vendor / "requirements.txt").read_text(encoding="utf-8") == "websockets>=15\n"


def test_add_no_deps_skips_uv_and_warns(workspace) -> None:
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))

    report = install.add(package, no_deps=True)

    assert report.vendor == {}
    assert report.warnings and "--no-deps" in report.warnings[0]
    assert not (workspace.plugins / "demo" / "_vendor").exists()


def test_add_registry_spec_materialises_runner_payload(workspace) -> None:
    payload = make_package(workspace.tmp / "payload")
    runner = FakeRunner(payload=payload)

    report = install.add("demo==1.0.0", runner=runner)

    assert report.plugin_id == "demo"
    assert (workspace.plugins / "demo" / "plugin.toml").is_file()
    assert runner.commands[0].args[-1] == "demo==1.0.0"
    assert "--no-deps" in runner.commands[0].args


def test_add_git_url_uses_runner(workspace) -> None:
    payload = make_package(workspace.tmp / "payload")
    runner = FakeRunner(payload=payload)

    report = install.add("https://example.invalid/demo.git", runner=runner)

    assert report.plugin_id == "demo"
    assert "https://example.invalid/demo.git" in runner.commands[0].args


def test_add_wheel_archive(workspace) -> None:
    source = make_package(workspace.tmp / "src")
    wheel = zip_archive(source, workspace.tmp / "demo-1.0.0-py3-none-any.whl")

    report = install.add(wheel)

    assert report.plugin_id == "demo"
    assert (workspace.plugins / "demo" / "plugin.toml").is_file()


def test_add_tar_archive_with_top_level_directory(workspace) -> None:
    source = make_package(workspace.tmp / "src")
    archive = tar_archive(source, workspace.tmp / "demo-1.0.0.tar.gz")

    report = install.add(archive)

    assert report.plugin_id == "demo"
    assert (workspace.plugins / "demo" / "demo_plugin.py").is_file()


def test_add_vendor_version_conflict_is_preflight(workspace) -> None:
    shared = make_package(workspace.plugins, "shared")
    write_dist(shared / "_vendor" / python_tag(), "websockets", "15.0")
    package = make_package(workspace.tmp / "src")
    write_dist(package / "_vendor" / python_tag(), "websockets", "15.1")

    with pytest.raises(PreflightFailure) as info:
        install.add(package)

    assert any(FAILURE_DEP_CONFLICT in problem for problem in info.value.problems)
    assert not (workspace.plugins / "demo").exists()


def test_add_post_install_conflict_discards_staging(workspace) -> None:
    shared = make_package(workspace.plugins, "shared")
    write_dist(shared / "_vendor" / python_tag(), "websockets", "15.0")
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    runner = FakeRunner(vendor={"websockets": "15.1"})

    with pytest.raises(PreflightFailure) as info:
        install.add(package, runner=runner)

    assert any(FAILURE_DEP_CONFLICT in problem for problem in info.value.problems)
    assert not (workspace.plugins / "demo").exists()
    assert staging_directories(workspace) == []


# -- remove ----------------------------------------------------------------


def test_remove_deletes_directory_manifest_and_caches(workspace, capsys) -> None:
    package = make_package(workspace.tmp / "src")
    install.add(package)
    cache = workspace.plugins / "demo" / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "stale.pyc").write_bytes(b"stale")

    assert cli.main(["remove", "demo"]) == cli.EXIT_RESTART

    assert not (workspace.plugins / "demo").exists()
    assert not workspace.manifest.exists()
    assert "restart deeppyro" in capsys.readouterr().out
    assert staging_directories(workspace) == []


def test_remove_unknown_plugin_is_usage(workspace, capsys) -> None:
    assert cli.main(["remove", "missing"]) == cli.EXIT_USAGE
    assert "not installed" in capsys.readouterr().err


def test_remove_dependents_requires_force(workspace) -> None:
    install.add(make_package(workspace.tmp / "src", "base"))
    install.add(make_package(workspace.tmp / "src", "dependent", requires=("base>=1",)), no_deps=True)

    with pytest.raises(PreflightFailure) as info:
        install.remove("base")

    assert info.value.problems == ("dependent depends on base",)
    assert (workspace.plugins / "base").exists()

    report = install.remove("base", force=True)

    assert report.dependents == ("dependent",)
    assert not (workspace.plugins / "base").exists()


# -- update ----------------------------------------------------------------


def test_update_rebuilds_requires_and_reports_diff(workspace) -> None:
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    install.add(package, no_deps=True)
    write_dist(workspace.plugins / "demo" / "_vendor" / python_tag(), "websockets", "15.0")
    runner = FakeRunner(vendor={"websockets": "15.1"})

    report = install.update("demo", runner=runner)

    assert report.rebuilt is True
    assert [(change.name, change.before, change.after) for change in report.changes] == [
        ("websockets", "15.0", "15.1")
    ]
    vendor = workspace.plugins / "demo" / "_vendor" / python_tag()
    assert vendor_versions(vendor) == {"websockets": "15.1"}
    assert not list(workspace.plugins.glob(".dpyro-update-*"))


def test_update_without_dependencies_is_a_warning(workspace) -> None:
    install.add(make_package(workspace.tmp / "src"))

    report = install.update("demo", runner=FakeRunner())

    assert report.rebuilt is False
    assert report.changes == ()
    assert report.warnings and "nothing to rebuild" in report.warnings[0]


def test_update_lock_mode_uses_lock_export_and_install(workspace) -> None:
    package = make_package(workspace.tmp / "src")
    (package / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '1.0.0'\n", encoding="utf-8")
    (package / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    install.add(package)
    runner = FakeRunner(vendor={"websockets": "15.1"})

    report = install.update("demo", runner=runner)

    assert [command.args[0] for command in runner.commands] == ["lock", "export", "pip"]
    assert report.rebuilt is True
    assert [(change.name, change.before, change.after) for change in report.changes] == [
        ("websockets", "", "15.1")
    ]
    vendor = workspace.plugins / "demo" / "_vendor" / python_tag()
    assert vendor_versions(vendor) == {"websockets": "15.1"}


def test_update_lock_failure_restores_uv_lock(workspace) -> None:
    package = make_package(workspace.tmp / "src")
    (package / "pyproject.toml").write_text("[project]\nname = 'demo'\nversion = '1.0.0'\n", encoding="utf-8")
    (package / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    install.add(package)
    lock = workspace.plugins / "demo" / "uv.lock"
    original = lock.read_bytes()
    runner = FakeRunner(fail_at="export")

    with pytest.raises(InstallFailure) as info:
        install.update("demo", runner=runner)

    assert info.value.rolled_back is True
    assert lock.read_bytes() == original
    assert not list(workspace.plugins.glob(".dpyro-update-*"))


def test_update_cli_prints_version_diff(workspace, capsys, monkeypatch) -> None:
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    install.add(package, no_deps=True)
    write_dist(workspace.plugins / "demo" / "_vendor" / python_tag(), "websockets", "15.0")
    runner = FakeRunner(vendor={"websockets": "15.1"})
    monkeypatch.setattr(install, "run_uv", lambda command, timeout=None, executable=None: runner(command))
    monkeypatch.setattr(install, "uv_executable", lambda: "uv")

    assert cli.main(["update", "demo"]) == cli.EXIT_RESTART

    out = capsys.readouterr().out
    assert "websockets: 15.0 -> 15.1" in out
    assert "updated demo" in out


# -- enable / disable ------------------------------------------------------


def test_disable_then_enable_toggles_manifest(workspace) -> None:
    install.add(make_package(workspace.tmp / "src"))

    assert install.set_enabled("demo", False).changed is True
    assert "disabled = true" in workspace.manifest.read_text(encoding="utf-8")
    assert install.set_enabled("demo", False).changed is False

    assert install.set_enabled("demo", True).changed is True
    assert "disabled = true" not in workspace.manifest.read_text(encoding="utf-8")


def test_cli_disable_and_enable_exit_codes(workspace, capsys) -> None:
    install.add(make_package(workspace.tmp / "src"))
    capsys.readouterr()

    assert cli.main(["disable", "demo"]) == cli.EXIT_RESTART
    assert "disabled demo" in capsys.readouterr().out
    assert cli.main(["disable", "demo"]) == cli.EXIT_OK
    assert cli.main(["enable", "demo"]) == cli.EXIT_RESTART


# -- allow-version ---------------------------------------------------------


def test_allow_version_writes_exempt_array_and_backup(workspace) -> None:
    directory = workspace.plugins / "demo"
    directory.mkdir(parents=True)
    original = plugin_toml("demo", dpyro=">=99").replace(
        '[compat]\ndpyro = ">=99"', '[compat]\ndpyro = ">=99"\ndpyro_exempt_versions = []'
    )
    (directory / "plugin.toml").write_text(original, encoding="utf-8")

    report = install.allow_version("demo", dpyro_version())

    assert report.changed is True
    toml_path = directory / "plugin.toml"
    assert f'dpyro_exempt_versions = ["{dpyro_version()}"]' in toml_path.read_text(encoding="utf-8")
    assert (directory / "plugin.toml.bak").read_text(encoding="utf-8") == original
    assert check_compat(load_metadata(toml_path, plugin_id="demo")) == []
    assert install.allow_version("demo", dpyro_version()).changed is False


def test_allow_version_cli_is_idempotent(workspace, capsys) -> None:
    directory = workspace.plugins / "demo"
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(plugin_toml("demo", dpyro=">=99"), encoding="utf-8")

    assert cli.main(["allow-version", "demo", dpyro_version()]) == cli.EXIT_RESTART
    assert "backup: plugin.toml.bak" in capsys.readouterr().out
    assert cli.main(["allow-version", "demo", dpyro_version()]) == cli.EXIT_OK
    assert "already allowed" in capsys.readouterr().out


def test_allow_version_unknown_plugin_is_usage(workspace) -> None:
    with pytest.raises(UnknownPlugin):
        install.allow_version("missing", "0.1.0")


def test_with_exempt_version_creates_compat_table() -> None:
    updated = with_exempt_version('[package]\nid = "demo"\n', "0.2.0")

    assert "[compat]" in updated
    assert 'dpyro_exempt_versions = ["0.2.0"]' in updated


def test_with_exempt_version_preserves_multiline_style() -> None:
    text = "[compat]\ndpyro_exempt_versions = [\n    '0.1.0',\n]\n"

    updated = with_exempt_version(text, "0.2.0")

    assert updated == "[compat]\ndpyro_exempt_versions = [\n    '0.1.0',\n    '0.2.0',\n]\n"


def test_with_exempt_version_is_idempotent() -> None:
    text = '[compat]\ndpyro_exempt_versions = ["0.2.0"]\n'

    assert with_exempt_version(text, "0.2.0") == text


# -- doctor: conflicts, ABI, customized core -------------------------------


def test_doctor_reports_cross_package_dep_conflict(workspace, capsys) -> None:
    alpha = make_package(workspace.plugins, "alpha")
    write_dist(alpha / "_vendor" / python_tag(), "websockets", "15.0")
    beta = make_package(workspace.plugins, "beta")
    write_dist(beta / "_vendor" / python_tag(), "websockets", "15.1")

    assert cli.main(["doctor"]) == cli.EXIT_PREFLIGHT

    out = capsys.readouterr().out
    assert out.count(FAILURE_DEP_CONFLICT) >= 2
    assert "alpha" in out and "beta" in out


def test_list_json_reports_conflict_errors(workspace, capsys) -> None:
    alpha = make_package(workspace.plugins, "alpha")
    write_dist(alpha / "_vendor" / python_tag(), "websockets", "15.0")
    beta = make_package(workspace.plugins, "beta")
    write_dist(beta / "_vendor" / python_tag(), "websockets", "15.1")

    assert cli.main(["list", "--json"]) == cli.EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    by_id = {item["id"]: item for item in payload}
    assert by_id["alpha"]["healthy"] is False
    assert by_id["beta"]["healthy"] is False
    kinds = {error["kind"] for error in by_id["alpha"]["errors"]}
    assert FAILURE_DEP_CONFLICT in kinds


def test_doctor_reports_abi_mismatch(workspace, capsys) -> None:
    package = make_package(workspace.plugins, "binary")
    foreign = package / "_vendor" / "cp312"
    foreign.mkdir(parents=True)
    (foreign / "speedups.cp312-win_amd64.pyd").write_bytes(b"binary")

    assert cli.main(["doctor"]) == cli.EXIT_PREFLIGHT

    out = capsys.readouterr().out
    assert "abi-mismatch" in out
    assert "cp312" in out


def test_doctor_reports_missing_vendor_dependency(workspace, capsys) -> None:
    make_package(workspace.plugins, "needy", requires=("websockets>=15",))

    assert cli.main(["doctor"]) == cli.EXIT_PREFLIGHT

    out = capsys.readouterr().out
    assert FAILURE_DEP_MISSING in out
    assert "websockets>=15" in out


CORE_OVERRIDE_BUNDLE = """
[bundle]
id = "demo"
version = "1.0.0"
api_version = 3

[[plugins]]
id = "kernel"
plugin = "deeppyro.kernel.runtime:KernelRuntime"
config = { testOverride = true }
"""


def test_doctor_reports_customized_core_as_advisory(workspace, capsys) -> None:
    package = make_package(workspace.plugins, "demo")
    (package / "bundle.toml").write_text(CORE_OVERRIDE_BUNDLE, encoding="utf-8")

    assert cli.main(["doctor"]) == cli.EXIT_OK

    out = capsys.readouterr().out
    assert "customized-core" in out
    assert "demo" in out


def test_doctor_json_marks_customized_core(workspace, capsys) -> None:
    package = make_package(workspace.plugins, "demo")
    (package / "bundle.toml").write_text(CORE_OVERRIDE_BUNDLE, encoding="utf-8")

    assert cli.main(["doctor", "--json"]) == cli.EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "demo"
    assert payload["customizedCore"] is True
    assert payload["healthy"] is True


# -- uv process handling ---------------------------------------------------


def test_run_uv_keeps_only_the_last_stderr_lines() -> None:
    code = "import sys; [sys.stderr.write(f'line{i}\\n') for i in range(300)]"

    result = install.run_uv(UvCommand(("-c", code)), executable=sys.executable)

    assert result.ok
    lines = result.stderr.splitlines()
    assert len(lines) == install.STDERR_TAIL_LINES
    assert lines[0] == "line100"
    assert "line99" not in lines


def test_run_uv_timeout_returns_124() -> None:
    command = UvCommand(("-c", "import time; time.sleep(5)"))

    result = install.run_uv(command, timeout=0.2, executable=sys.executable)

    assert result.returncode == 124
    assert result.timed_out is True
    assert "timed out" in result.stderr


def test_run_uv_missing_executable_returns_127(workspace) -> None:
    result = install.run_uv(
        UvCommand(("--version",)),
        executable=str(workspace.tmp / "missing-uv"),
    )

    assert result.returncode == 127
    assert "cannot run" in result.stderr


def test_dpyro_uv_environment_override(workspace, monkeypatch) -> None:
    monkeypatch.setenv("DPYRO_UV", sys.executable)

    assert install.uv_executable() == sys.executable
    result = install.run_uv(UvCommand(("-c", "print('hello uv')")))
    assert result.stdout.strip() == "hello uv"


def test_uv_executable_error_when_missing(monkeypatch) -> None:
    monkeypatch.delenv("DPYRO_UV", raising=False)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)

    with pytest.raises(install.InstallerError):
        install.uv_executable()


# -- source planning -------------------------------------------------------


def test_parse_requirement_strips_extras_and_markers() -> None:
    assert install.parse_requirement("websockets[extra]>=15; python_version > '3'") == ("websockets", ">=15")
    assert install.parse_requirement("demo @ https://example.invalid/demo.whl") == ("demo", "")


def test_parse_requirement_rejects_unsupported_spec() -> None:
    with pytest.raises(ValueError):
        install.parse_requirement("demo~=1.0")


def test_plan_source_registry_and_missing_path(workspace) -> None:
    plan = install.plan_source("websockets>=15")
    assert plan.kind == "registry"
    assert plan.spec == "websockets>=15"
    assert plan.expected_id == "websockets"

    with pytest.raises(PreflightFailure):
        install.plan_source(str(workspace.tmp / "does-not-exist"))


def test_plan_source_rejects_invalid_directory_name(workspace) -> None:
    directory = workspace.tmp / "bad id"
    directory.mkdir()
    (directory / "plugin.toml").write_text(plugin_toml("bad id"), encoding="utf-8")

    with pytest.raises(PreflightFailure) as info:
        install.plan_source(str(directory))

    assert any("bad id" in problem for problem in info.value.problems)


# -- CLI failure payloads --------------------------------------------------


def test_add_json_success_payload(workspace, capsys) -> None:
    package = make_package(workspace.tmp / "src")

    assert cli.main(["add", str(package), "--json"]) == cli.EXIT_RESTART

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["id"] == "demo"
    assert payload["manifestChanged"] is True
    assert payload["restartRequired"] is True


def test_add_json_preflight_failure_payload(workspace, capsys) -> None:
    package = make_package(workspace.tmp / "src", dpyro=">=99")

    assert cli.main(["add", str(package), "--json"]) == cli.EXIT_PREFLIGHT

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["kind"] == "preflight"
    assert any("version-incompat" in problem for problem in payload["problems"])


def test_remove_cli_preflight_dependents(workspace, capsys) -> None:
    install.add(make_package(workspace.tmp / "src", "base"))
    install.add(make_package(workspace.tmp / "src", "dependent", requires=("base>=1",)), no_deps=True)

    assert cli.main(["remove", "base"]) == cli.EXIT_PREFLIGHT

    err = capsys.readouterr().err
    assert "dependent depends on base" in err
    assert (workspace.plugins / "base").exists()


def test_allow_version_rejects_invalid_version(workspace) -> None:
    directory = workspace.plugins / "demo"
    directory.mkdir(parents=True)
    (directory / "plugin.toml").write_text(plugin_toml("demo"), encoding="utf-8")

    with pytest.raises(PreflightFailure):
        install.allow_version("demo", "not a version")


# -- import validation -----------------------------------------------------


def test_add_preflight_rejects_missing_module(workspace) -> None:
    package = make_package(workspace.tmp / "src")
    (package / "demo_plugin.py").unlink()

    with pytest.raises(PreflightFailure) as info:
        install.add(package)

    assert any("import-failed" in problem for problem in info.value.problems)
    assert not (workspace.plugins / "demo").exists()
    assert not workspace.manifest.exists()


def test_add_preflight_rejects_non_plugin_class(workspace) -> None:
    package = make_package(workspace.tmp / "src")
    (package / "demo_plugin.py").write_text(
        'class DemoPlugin:\n    provide = "demo_service"\n', encoding="utf-8"
    )

    with pytest.raises(PreflightFailure) as info:
        install.add(package)

    assert any("does not subclass" in problem for problem in info.value.problems)
    assert not (workspace.plugins / "demo").exists()


def test_add_defers_missing_dependency_until_after_install(workspace) -> None:
    package = make_package(workspace.tmp / "src", requires=("websockets>=15",))
    (package / "demo_plugin.py").write_text(
        "import websockets\n\n"
        "from deeppyro.api import Plugin\n\n\n"
        "class DemoPlugin(Plugin):\n"
        '    provide = "demo_service"\n',
        encoding="utf-8",
    )

    report = install.add(package, runner=FakeRunner(vendor={"websockets": "15.1"}))

    assert report.plugin_id == "demo"
    assert report.vendor == {"websockets": "15.1"}
    assert (workspace.plugins / "demo" / "plugin.toml").is_file()


def test_add_post_commit_validation_failure_rolls_back(workspace, monkeypatch) -> None:
    package = make_package(workspace.tmp / "src")
    real = install.target_errors

    def flaky(metadata, directory, *, defer_missing=False):
        if not defer_missing:
            return (install.MetadataError("import-failed", "simulated vendor gap"),)
        return real(metadata, directory, defer_missing=defer_missing)

    monkeypatch.setattr(install, "target_errors", flaky)

    with pytest.raises(InstallFailure) as info:
        install.add(package)

    assert info.value.rolled_back is True
    assert not (workspace.plugins / "demo").exists()
    assert not workspace.manifest.exists()
    assert staging_directories(workspace) == []


# -- dangling manifest rows ------------------------------------------------


def test_doctor_reports_manifest_dangling(workspace, capsys) -> None:
    workspace.manifest.write_text(
        '[[plugins]]\nid = "ghost"\nname = "ghost_plugin:Ghost"\n', encoding="utf-8"
    )

    assert cli.main(["doctor"]) == cli.EXIT_PREFLIGHT

    out = capsys.readouterr().out
    assert "manifest-dangling" in out
    assert "ghost" in out
    assert "remove the row" in out


def test_doctor_json_payload_for_manifest_dangling(workspace, capsys) -> None:
    workspace.manifest.write_text(
        '[[plugins]]\nid = "ghost"\nname = "ghost_plugin:Ghost"\n', encoding="utf-8"
    )

    assert cli.main(["doctor", "--json"]) == cli.EXIT_PREFLIGHT

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "ghost"
    assert payload["manifestDangling"] is True
    assert payload["healthy"] is False


def test_doctor_reports_unimportable_manifest_target(workspace, capsys) -> None:
    make_package(workspace.plugins)
    workspace.manifest.write_text(
        '[[plugins]]\nid = "demo"\nname = "missing_module:DemoPlugin"\n', encoding="utf-8"
    )

    assert cli.main(["doctor"]) == cli.EXIT_PREFLIGHT

    out = capsys.readouterr().out
    assert "cannot import target" in out


def test_remove_drops_dangling_manifest_row(workspace) -> None:
    workspace.manifest.write_text(
        '[[plugins]]\nid = "ghost"\nname = "ghost_plugin:Ghost"\n'
        '[[plugins]]\nid = "keep"\nname = "keep_plugin:Keep"\n',
        encoding="utf-8",
    )

    report = install.remove("ghost")

    assert report.manifest_changed is True
    remaining = workspace.manifest.read_text(encoding="utf-8")
    assert "ghost" not in remaining
    assert "keep" in remaining
    with pytest.raises(UnknownPlugin):
        install.remove("ghost")


def test_remove_cli_drops_dangling_manifest_row(workspace, capsys) -> None:
    workspace.manifest.write_text(
        '[[plugins]]\nid = "ghost"\nname = "ghost_plugin:Ghost"\n', encoding="utf-8"
    )

    assert cli.main(["remove", "ghost"]) == cli.EXIT_RESTART

    remaining = workspace.manifest.read_text(encoding="utf-8") if workspace.manifest.exists() else ""
    assert "ghost" not in remaining
    assert "restart deeppyro" in capsys.readouterr().out


async def test_boot_survives_dangling_manifest_row(workspace, tmp_path) -> None:
    from deeppyro.config import Config
    from deeppyro.kernel.app import create_app
    from deeppyro.kernel.manifest import PluginSpec

    manifest = tmp_path / "boot.toml"
    manifest.write_text('[[plugins]]\nid = "ghost"\nname = "ghost_missing:Ghost"\n', encoding="utf-8")
    config = Config.model_validate(
        {
            "agent": {"provider": "local", "model": "m"},
            "providers": {
                "local": {
                    "api": "openai",
                    "baseURL": "http://127.0.0.1:8000/v1",
                    "models": [{"id": "m"}],
                }
            },
        }
    )

    app = await create_app(
        config,
        plugins=(PluginSpec(id="counter", name="tests.kernel.plugins_dummy:CounterPlugin"),),
        manifest_paths=(str(manifest),),
    )
    try:
        states = {entry["id"]: entry for entry in app.row_states()}
        assert states["counter"]["state"] == "active"
        assert states["ghost"]["state"] == "failed"
        assert "ghost_missing" in states["ghost"]["error"]
    finally:
        await app.stop()
