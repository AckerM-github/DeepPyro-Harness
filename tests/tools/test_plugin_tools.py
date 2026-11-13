"""Tests for the agent-facing plugin tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deeppyro.plugins import BundleInfo, PluginInfo
from deeppyro.tools.builtin.plugin_tools import (
    PluginInspectTool,
    PluginManagerTool,
    PluginToolsPlugin,
    SettingsReadTool,
    SettingsWriteTool,
    plugin_tools,
)
from tests.tools.helpers import make_context

BUNDLE = BundleInfo(
    id="demo-bundle",
    name="Demo Bundle",
    version="0.1.0",
    api_version=1,
    order=500,
    critical=False,
    path="C:/home/bundles/demo-bundle/bundle.toml",
    enabled=True,
    state="loaded",
    row_ids=("demo-plugin",),
)
PLUGIN = PluginInfo(
    id="demo-plugin",
    name="Demo Plugin",
    version="0.1.0",
    source="manifest",
    origin="C:/home/plugins.toml",
    description="a demo plugin",
    enabled=True,
    target="tests.builtin.demo_plugins:DemoPlugin",
)


class FakeManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: Exception | None = None

    def list_bundles(self) -> list[BundleInfo]:
        self.calls.append(("list_bundles", None))
        return [BUNDLE]

    def list(self) -> list[PluginInfo]:
        self.calls.append(("list", None))
        return [PLUGIN]

    def install_bundle(self, source: str, *, force: bool = False) -> BundleInfo:
        self.calls.append(("install_bundle", (source, force)))
        if self.fail is not None:
            raise self.fail
        return BUNDLE

    def set_bundle(self, bundle_id: str, enabled: bool) -> BundleInfo:
        self.calls.append(("set_bundle", (bundle_id, enabled)))
        return BUNDLE

    def remove_bundle(self, bundle_id: str) -> BundleInfo:
        self.calls.append(("remove_bundle", bundle_id))
        return BUNDLE


class FakeInspect:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: Exception | None = None

    def _record(self, name: str, value: Any) -> list[dict[str, Any]]:
        self.calls.append((name, value))
        if self.fail is not None:
            raise self.fail
        return [{"name": name}]

    def services(self) -> list[dict[str, Any]]:
        return self._record("services", None)

    def plugins(self) -> list[dict[str, Any]]:
        return self._record("plugins", None)

    def tools(self, workspace: Any = None) -> list[dict[str, Any]]:
        return self._record("tools", workspace)

    def prompt_sections(self) -> list[dict[str, Any]]:
        return self._record("prompt_sections", None)

    def commands(self) -> list[dict[str, Any]]:
        return self._record("commands", None)

    def providers(self) -> list[dict[str, Any]]:
        return self._record("providers", None)


class FakeSettings:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: Exception | None = None

    def read(self, plugin_id: str) -> dict[str, Any]:
        self.calls.append(("read", plugin_id))
        if self.fail is not None:
            raise self.fail
        return {"plugin_id": plugin_id, "values": {"apiKey": "***"}, "revision": "abc"}

    def write(self, plugin_id: str, patch: dict[str, Any], expected_revision: str) -> dict[str, Any]:
        self.calls.append(("write", (plugin_id, patch, expected_revision)))
        if self.fail is not None:
            raise self.fail
        return {"plugin_id": plugin_id, "changed": sorted(patch), "revision": "def"}


def context(tmp_path: Path, **services: Any):
    return make_context(tmp_path, services=services)


async def test_manager_list_returns_bundles_and_plugins(tmp_path: Path) -> None:
    manager = FakeManager()
    result = await PluginManagerTool().run({"action": "list"}, context(tmp_path, plugin_manager=manager))

    assert not result.is_error
    payload = json.loads(result.text)
    assert payload["bundles"][0]["id"] == "demo-bundle"
    assert payload["plugins"][0]["id"] == "demo-plugin"
    assert manager.calls == [("list_bundles", None), ("list", None)]


async def test_manager_install_requires_source(tmp_path: Path) -> None:
    result = await PluginManagerTool().run(
        {"action": "install"}, context(tmp_path, plugin_manager=FakeManager())
    )
    assert result.is_error
    assert "source" in result.text


async def test_manager_install_forwards_force(tmp_path: Path) -> None:
    manager = FakeManager()
    result = await PluginManagerTool().run(
        {"action": "install", "source": "C:/bundles/demo", "force": True},
        context(tmp_path, plugin_manager=manager),
    )
    assert not result.is_error
    assert manager.calls == [("install_bundle", ("C:/bundles/demo", True))]
    assert json.loads(result.text)["id"] == "demo-bundle"


async def test_manager_set_requires_boolean_enabled(tmp_path: Path) -> None:
    result = await PluginManagerTool().run(
        {"action": "set", "id": "demo-bundle", "enabled": "yes"},
        context(tmp_path, plugin_manager=FakeManager()),
    )
    assert result.is_error
    assert "boolean" in result.text


async def test_manager_set_and_remove(tmp_path: Path) -> None:
    manager = FakeManager()
    tool = PluginManagerTool()
    ctx = context(tmp_path, plugin_manager=manager)

    assert not (await tool.run({"action": "set", "id": "demo-bundle", "enabled": False}, ctx)).is_error
    assert not (await tool.run({"action": "remove", "id": "demo-bundle"}, ctx)).is_error
    assert manager.calls == [
        ("set_bundle", ("demo-bundle", False)),
        ("remove_bundle", "demo-bundle"),
    ]


@pytest.mark.parametrize("args", [{"action": "remove"}, {"action": "bogus"}])
async def test_manager_rejects_bad_requests(tmp_path: Path, args: dict[str, Any]) -> None:
    result = await PluginManagerTool().run(args, context(tmp_path, plugin_manager=FakeManager()))
    assert result.is_error


async def test_manager_reports_service_failures(tmp_path: Path) -> None:
    manager = FakeManager()
    manager.fail = RuntimeError("boom")
    result = await PluginManagerTool().run(
        {"action": "install", "source": "x"},
        context(tmp_path, plugin_manager=manager),
    )
    assert result.is_error
    assert "plugin_manager failed: boom" in result.text


async def test_missing_services_are_reported(tmp_path: Path) -> None:
    for tool, args in (
        (PluginManagerTool(), {"action": "list"}),
        (PluginInspectTool(), {"what": "services"}),
        (SettingsReadTool(), {"plugin_id": "llm"}),
        (SettingsWriteTool(), {"plugin_id": "llm", "patch": {}, "expected_revision": "x"}),
    ):
        result = await tool.run(args, context(tmp_path))
        assert result.is_error
        assert "not available in this session" in result.text


@pytest.mark.parametrize(
    ("what", "method"),
    [
        ("services", "services"),
        ("plugins", "plugins"),
        ("tools", "tools"),
        ("prompts", "prompt_sections"),
        ("commands", "commands"),
        ("providers", "providers"),
    ],
)
async def test_inspect_dispatches_what(tmp_path: Path, what: str, method: str) -> None:
    inspect = FakeInspect()
    workspace = str(tmp_path / "ws")
    args: dict[str, Any] = {"what": what}
    if what == "tools":
        args["workspace"] = workspace
    result = await PluginInspectTool().run(args, context(tmp_path, inspect=inspect))

    assert not result.is_error
    assert inspect.calls[0][0] == method
    if what == "tools":
        assert inspect.calls[0][1] == workspace
    assert json.loads(result.text) == [{"name": method}]


async def test_inspect_rejects_unknown_what(tmp_path: Path) -> None:
    result = await PluginInspectTool().run({"what": "nope"}, context(tmp_path, inspect=FakeInspect()))
    assert result.is_error
    assert "unknown plugin_inspect target" in result.text


async def test_inspect_reports_service_failures(tmp_path: Path) -> None:
    inspect = FakeInspect()
    inspect.fail = RuntimeError("inspect broke")
    result = await PluginInspectTool().run({"what": "services"}, context(tmp_path, inspect=inspect))
    assert result.is_error
    assert "plugin_inspect failed: inspect broke" in result.text


async def test_settings_read_returns_masked_payload(tmp_path: Path) -> None:
    settings = FakeSettings()
    result = await SettingsReadTool().run({"plugin_id": "demo-plugin"}, context(tmp_path, settings=settings))

    assert not result.is_error
    assert json.loads(result.text)["values"]["apiKey"] == "***"
    assert settings.calls == [("read", "demo-plugin")]


@pytest.mark.parametrize("args", [{}, {"plugin_id": "  "}])
async def test_settings_read_requires_plugin_id(tmp_path: Path, args: dict[str, Any]) -> None:
    result = await SettingsReadTool().run(args, context(tmp_path, settings=FakeSettings()))
    assert result.is_error
    assert "plugin_id" in result.text


async def test_settings_write_validates_arguments(tmp_path: Path) -> None:
    tool = SettingsWriteTool()
    ctx = context(tmp_path, settings=FakeSettings())

    assert (await tool.run({"plugin_id": "x", "patch": ["bad"], "expected_revision": "r"}, ctx)).is_error
    assert (await tool.run({"plugin_id": "x", "patch": {}, "expected_revision": 1}, ctx)).is_error


async def test_settings_write_returns_changes(tmp_path: Path) -> None:
    settings = FakeSettings()
    result = await SettingsWriteTool().run(
        {"plugin_id": "demo-plugin", "patch": {"model": "m2"}, "expected_revision": "abc"},
        context(tmp_path, settings=settings),
    )

    assert not result.is_error
    assert json.loads(result.text)["changed"] == ["model"]
    assert settings.calls == [("write", ("demo-plugin", {"model": "m2"}, "abc"))]


async def test_settings_write_reports_conflicts(tmp_path: Path) -> None:
    settings = FakeSettings()
    settings.fail = RuntimeError("stale settings revision")
    result = await SettingsWriteTool().run(
        {"plugin_id": "demo-plugin", "patch": {}, "expected_revision": "old"},
        context(tmp_path, settings=settings),
    )
    assert result.is_error
    assert "stale settings revision" in result.text


def test_tool_set_metadata() -> None:
    tools = plugin_tools()
    assert [tool.name for tool in tools] == [
        "plugin_manager",
        "plugin_inspect",
        "settings_read",
        "settings_write",
    ]
    assert all(tool.parallel_safe is False for tool in tools)
    assert "human approval" in tools[0].description


def test_plugin_tools_plugin_registers_factory() -> None:
    registered: list[Any] = []

    class FakeContext:
        def register_tool(self, factory: Any) -> None:
            registered.append(factory)

    plugin = PluginToolsPlugin.__new__(PluginToolsPlugin)
    plugin.setup(FakeContext(), None)

    assert len(registered) == 1
    tools = registered[0](None, None)
    assert [tool.name for tool in tools] == [
        "plugin_manager",
        "plugin_inspect",
        "settings_read",
        "settings_write",
    ]
