"""Plugin CLI capability output: manifests, missing deps, dependency tree.

Covers KI-019 (host-style plugins get a derivable CapabilityManifest that the
CLI can show) and KI-021c (the plugin CLI exposes capabilities).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.plugins import cli
from tests.plugins.support import write_plugin

MISSING_DEP_MODULE = """\
from deeppyro.api import Plugin


class NeedsServicePlugin(Plugin):
    plugin_id = "needs-service"
    version = "0.4.0"
    inject = ("absent-service",)
"""


class CLIError(Exception):
    """Marker for readable assertion messages."""


@pytest.fixture()
def plugins_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPPYRO_PLUGINS_DIR", str(plugins))
    monkeypatch.setenv("DEEPPYRO_PLUGINS_MANIFEST", str(tmp_path / "plugins.toml"))
    return plugins


def _run(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, str, str]:
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_list_json_reports_manifest_and_missing_dependencies(
    plugins_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin(plugins_root / "demo-plugin", provides=("demo",), class_provide="demo")

    code, out, _err = _run(capsys, ["list", "--json"])

    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert isinstance(payload, list) and payload
    row = payload[0]
    assert row["id"] == "demo-plugin"
    manifest = row["manifest"]
    assert manifest["plugin_id"] == "DemoPlugin"
    assert manifest["services_provided"] == ["demo"]
    assert manifest["provides"] == ["demo"]
    assert row["missingDependencies"] == []
    assert row["capabilityIssues"] == []


def test_inspect_json_shows_manifest_and_dependency_tree(
    plugins_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin(plugins_root / "demo-plugin", provides=("demo",), class_provide="demo")

    code, out, _err = _run(capsys, ["inspect", "demo-plugin", "--json"])

    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["id"] == "demo-plugin"
    assert payload["manifest"]["plugin_id"] == "DemoPlugin"
    assert payload["missingDependencies"] == []
    tree = payload["dependencyTree"]
    assert tree["pluginId"] == "demo-plugin"
    assert tree["provides"] == ["demo"]
    assert tree["requires"] == []
    assert tree["missing"] is False


def test_inspect_json_lists_unprovided_requirements(
    plugins_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin(
        plugins_root / "needs-plugin",
        name="Needs Service",
        module="needs_plugin_module",
        class_name="NeedsServicePlugin",
        provides=None,
        module_source=MISSING_DEP_MODULE,
    )

    code, out, _err = _run(capsys, ["inspect", "needs-plugin", "--json"])

    assert code == cli.EXIT_OK
    payload = json.loads(out)
    assert payload["id"] == "needs-plugin"
    assert payload["manifest"]["requires"] == ["absent-service"]
    assert payload["missingDependencies"] == ["absent-service"]
    requirement = payload["dependencyTree"]["requires"][0]
    assert requirement["service"] == "absent-service"
    assert requirement["provider"] is None
    assert requirement["satisfied"] is False
    kinds = {issue["kind"] for issue in payload["capabilityIssues"]}
    assert "missing-require" in kinds


def test_inspect_human_output_prints_capabilities(
    plugins_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_plugin(plugins_root / "demo-plugin", provides=("demo",), class_provide="demo")

    code, out, _err = _run(capsys, ["inspect", "demo-plugin"])

    assert code == cli.EXIT_OK
    assert "plugin DemoPlugin" in out
    assert "provides:" in out
    assert "demo" in out
    assert "dependency tree:" in out


def test_inspect_unknown_plugin_is_usage_error(
    plugins_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = _run(capsys, ["inspect", "not-installed"])

    assert code == cli.EXIT_USAGE
    assert "not-installed" in err
