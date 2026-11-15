"""Plugin packages: metadata parsing, bootstrap activation, CLI and layering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.plugins import bootstrap, cli
from deeppyro.plugins.metadata import (
    FAILURE_DEP_MISSING,
    FAILURE_INCOMPAT,
    FAILURE_METADATA,
    MetadataError,
    check_compat,
    load_metadata,
    parse_metadata,
    satisfies,
)

V2 = """
[package]
id = "demo"
name = "Demo Plugin"
version = "1.2.3"
description = "a fixture"
module = "dpyro_demo"
class = "DemoPlugin"
api_version = 1
provides = ["demo_service"]

[compat]
dpyro = ">=0.1,<1.0"
python = ">=3.12"

[deps]
requires = ["websockets>=15"]

[ui]
slots = ["status", "panel"]

[bundle]
patch = "bundle.toml"
"""


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


NO_DEPS = V2.replace('[deps]\nrequires = ["websockets>=15"]\n', "")


def make_package(
    root: Path,
    plugin_id: str,
    text: str = V2,
    *,
    module: str | None = None,
) -> Path:
    directory = root / plugin_id
    write(directory / "plugin.toml", text.replace('id = "demo"', f'id = "{plugin_id}"'))
    if module:
        write(directory / f"{module}.py", "class DemoPlugin:\n    pass\n")
    return directory


# -- metadata --------------------------------------------------------------


def test_parse_v2_sections(tmp_path: Path) -> None:
    directory = make_package(tmp_path, "demo")
    metadata = load_metadata(directory)
    assert metadata.id == "demo"
    assert metadata.target == "dpyro_demo:DemoPlugin"
    assert metadata.provides == ("demo_service",)
    assert metadata.deps.requires == ("websockets>=15",)
    assert metadata.compat.dpyro == ">=0.1,<1.0"
    assert metadata.ui.slots == ("status", "panel")
    assert metadata.bundle.patch == "bundle.toml"


def test_module_colon_form_and_legacy_flat_keys(tmp_path: Path) -> None:
    document = {
        "name": "Legacy",
        "version": "0.0.1",
        "description": "flat",
        "module": "legacy_plugin:LegacyPlugin",
        "api_version": 1,
        "provides": ["legacy_service"],
    }
    metadata = parse_metadata(document, plugin_id="legacy", directory=tmp_path)
    assert metadata.module == "legacy_plugin"
    assert metadata.class_name == "LegacyPlugin"
    assert metadata.target == "legacy_plugin:LegacyPlugin"
    assert metadata.provides == ("legacy_service",)


def test_missing_api_version_is_metadata_invalid(tmp_path: Path) -> None:
    directory = tmp_path / "broken"
    write(directory / "plugin.toml", '[package]\nname = "x"\nmodule = "m"\nclass = "C"\n')
    with pytest.raises(MetadataError) as info:
        load_metadata(directory)
    assert info.value.kind == FAILURE_METADATA
    assert "api_version" in info.value.message


def test_directory_name_must_match_package_id(tmp_path: Path) -> None:
    directory = make_package(tmp_path, "renamed")
    with pytest.raises(MetadataError) as info:
        load_metadata(directory, plugin_id="other")
    assert info.value.kind == FAILURE_METADATA


def test_utf8_bom_is_tolerated(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / "plugin.toml").write_bytes(b"\xef\xbb\xbf" + V2.encode("utf-8"))
    assert load_metadata(directory).id == "demo"


def test_unknown_slot_kind_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "demo"
    write(directory / "plugin.toml", V2.replace('slots = ["status", "panel"]', 'slots = ["sidebar"]'))
    with pytest.raises(MetadataError):
        load_metadata(directory, known_slots=("status", "panel"))


def test_satisfies_handles_ranges_and_rejects_bad_specs() -> None:
    assert satisfies("0.1.0", ">=0.1,<0.2")
    assert not satisfies("0.2.0", ">=0.1,<0.2")
    assert satisfies("3.13.18", ">=3.12")
    assert satisfies("0.1.0", "")
    with pytest.raises(ValueError):
        satisfies("0.1.0", "~=0.1")


def test_check_compat_reports_python_and_host_ranges(tmp_path: Path) -> None:
    directory = make_package(tmp_path, "demo")
    metadata = load_metadata(directory)
    assert check_compat(metadata, python_version=(3, 13, 0), host_version="0.1.0") == []
    mismatched = [error for error in check_compat(metadata, python_version=(3, 10, 0), host_version="0.1.0")]
    assert [error.kind for error in mismatched] == [FAILURE_INCOMPAT]
    hosted = check_compat(metadata, python_version=(3, 13, 0), host_version="2.0.0")
    assert [error.kind for error in hosted] == [FAILURE_INCOMPAT]


# -- bootstrap -------------------------------------------------------------


def test_scan_reports_broken_packages_without_raising(tmp_path: Path) -> None:
    make_package(tmp_path, "demo")
    broken = tmp_path / "broken"
    write(broken / "plugin.toml", '[package]\nmodule = "m"\nclass = "C"\n')
    (tmp_path / "not-a-plugin").mkdir()

    packages = bootstrap.scan_plugin_packages(tmp_path)
    assert [package.id for package in packages] == ["broken", "demo"]
    assert packages[0].errors and packages[0].failure_kind() == FAILURE_METADATA
    assert packages[1].loadable


def test_activate_puts_package_and_vendor_on_path(tmp_path: Path) -> None:
    directory = make_package(tmp_path, "demo")
    vendor = directory / "_vendor" / "cp313"
    vendor.mkdir(parents=True)

    packages = bootstrap.scan_plugin_packages(tmp_path)
    path: list[str] = []
    activation = bootstrap.activate(packages, path=path)

    assert path == [str(directory), str(vendor)]
    assert activation.added == [str(directory), str(vendor)]
    assert activation.get("demo") is packages[0]
    # Idempotent: a second activation adds nothing.
    assert bootstrap.activate(packages, path=path).added == []


def test_activation_orders_vendor_after_its_package(tmp_path: Path) -> None:
    first = make_package(tmp_path, "aaa")
    second = make_package(tmp_path, "bbb")
    (first / "_vendor" / "cp313").mkdir(parents=True)
    (second / "_vendor" / "cp313").mkdir(parents=True)

    packages = bootstrap.scan_plugin_packages(tmp_path)
    assert bootstrap.activation_paths(packages) == [
        str(first),
        str(first / "_vendor" / "cp313"),
        str(second),
        str(second / "_vendor" / "cp313"),
    ]


def test_load_package_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        bootstrap.load_package("absent", tmp_path)


# -- diagnosis and CLI -----------------------------------------------------


def test_dependency_errors_name_the_missing_requirement(tmp_path: Path) -> None:
    directory = make_package(tmp_path, "demo")
    (directory / "_vendor" / "cp313" / "websockets").mkdir(parents=True)
    write(directory / "_vendor" / "cp313" / "websockets" / "__init__.py", "")
    package = bootstrap.load_package("demo", tmp_path)
    assert cli.dependency_errors(package) == []

    (directory / "_vendor" / "cp313" / "websockets").rename(directory / "_vendor" / "cp313" / "ws")
    errors = cli.dependency_errors(package)
    assert [error.kind for error in errors] == [FAILURE_DEP_MISSING]
    assert "websockets" in errors[0].message


def test_doctor_exit_code_and_output(tmp_path: Path, monkeypatch, capsys) -> None:
    make_package(tmp_path, "demo", NO_DEPS)
    broken = tmp_path / "broken"
    write(broken / "plugin.toml", '[package]\nmodule = "m"\nclass = "C"\n')
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(tmp_path))

    assert cli.main(["doctor"]) == cli.EXIT_PREFLIGHT
    output = capsys.readouterr().out
    assert "FAIL  broken" in output
    assert "metadata-invalid" in output
    assert "ok  demo 1.2.3" in output

    assert cli.main(["doctor", "demo"]) == cli.EXIT_OK


def test_list_json_payload(tmp_path: Path, monkeypatch, capsys) -> None:
    make_package(tmp_path, "demo", NO_DEPS)
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(tmp_path))

    assert cli.main(["list", "--json"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["id"] == "demo"
    assert payload[0]["healthy"] is True
    assert payload[0]["target"] == "dpyro_demo:DemoPlugin"


def test_info_verb_dumps_metadata(tmp_path: Path, monkeypatch, capsys) -> None:
    make_package(tmp_path, "demo")
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(tmp_path))

    assert cli.main(["info", "demo"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["deps"]["requires"] == ["websockets>=15"]
    assert payload["ui"]["slots"] == ["status", "panel"]


# -- composition layering --------------------------------------------------


def test_plugin_bundle_layer_is_applied_after_core(tmp_path: Path, monkeypatch) -> None:
    from deeppyro.kernel.composition import default_composition

    directory = make_package(tmp_path, "demo")
    write(
        directory / "bundle.toml",
        """
[bundle]
id = "demo"
version = "1.2.3"
api_version = 1

[[plugins]]
id = "demo-service"
plugin = "dpyro_demo:DemoPlugin"
config = { answer = 42 }
""",
    )
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(tmp_path))

    composition = default_composition()
    row = composition.get("demo-service")
    assert row is not None
    assert row.plugin == "dpyro_demo:DemoPlugin"
    assert row.config == {"answer": 42}


def test_plugin_bundle_may_override_core_config(tmp_path: Path, monkeypatch) -> None:
    from deeppyro.kernel.composition import default_composition

    directory = make_package(tmp_path, "demo")
    write(
        directory / "bundle.toml",
        """
[bundle]
id = "demo"
version = "1.2.3"
api_version = 1

[[plugins]]
id = "kernel"
plugin = "deeppyro.kernel.runtime:KernelRuntime"
config = { testOverride = true }
""",
    )
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(tmp_path))

    composition = default_composition()
    row = composition.get("kernel")
    assert row is not None
    assert row.overridden is True
    assert row.config == {"testOverride": True}
