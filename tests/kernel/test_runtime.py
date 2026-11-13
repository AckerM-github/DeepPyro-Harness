"""Tests for the kernel runtime service: mounted plugin registry and live config."""

from __future__ import annotations

from deeppyro.config import Config
from deeppyro.kernel.app import create_app
from deeppyro.kernel.composition import Composition, PluginRow
from deeppyro.kernel.runtime import KernelRuntime

DUMMY = "tests.kernel.plugins_dummy"
KERNEL_ROW = PluginRow(id="kernel", plugin="deeppyro.kernel.runtime:KernelRuntime")


def _config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "m"},
            "providers": {
                "local": {"api": "openai", "baseURL": "http://127.0.0.1:8000/v1", "models": [{"id": "m"}]}
            },
        }
    )


async def _boot(rows: tuple[PluginRow, ...]) -> object:
    composition = Composition(rows=(KERNEL_ROW, *rows), origin="test")
    return await create_app(_config(), composition=composition)


async def test_kernel_runtime_reports_mounted_plugins() -> None:
    app = await _boot((PluginRow(id="vol", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 2}),))
    try:
        assert app.mount_record("kernel").cls is KernelRuntime
        assert app.started["kernel"] == "ACTIVE"
        kernel = app.service("kernel")
        mounted = kernel.mounted_plugins()
        assert [plugin["id"] for plugin in mounted] == ["kernel", "vol"]
        assert {plugin["state"] for plugin in mounted} == {"active"}
        by_id = {plugin["id"]: plugin for plugin in mounted}
        assert by_id["kernel"]["plugin"] == "deeppyro.kernel.runtime:KernelRuntime"
        assert by_id["vol"]["config"] == {"threshold": 2, "label": "default"}
        assert by_id["vol"]["group"] is False
    finally:
        await app.stop()


async def test_apply_plugin_config_updates_volatile_field() -> None:
    app = await _boot((PluginRow(id="vol", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 2}),))
    try:
        kernel = app.service("kernel")
        assert kernel.apply_plugin_config("vol", {"threshold": 9}) is True
        record = app.mount_record("vol")
        assert record.instance.config.threshold == 9
        assert record.instance.hook_calls == [9]
        assert record.config == {"threshold": 9, "label": "default"}
        states = {state["id"]: state for state in app.row_states()}
        assert states["vol"]["state"] == "active"
        assert states["vol"]["config"]["threshold"] == 9
        assert app.restart_required == frozenset()
    finally:
        await app.stop()


async def test_apply_plugin_config_rejects_non_volatile_change() -> None:
    app = await _boot((PluginRow(id="vol", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 2}),))
    try:
        kernel = app.service("kernel")
        assert kernel.apply_plugin_config("vol", {"label": "other"}) is False
        assert app.restart_required == {"vol"}
        states = {state["id"]: state for state in app.row_states()}
        assert states["vol"]["state"] == "restart-required"
        record = app.mount_record("vol")
        assert record.instance.config.label == "default"
        assert record.instance.hook_calls == []
        assert record.config == {"threshold": 2, "label": "default"}
    finally:
        await app.stop()


async def test_apply_plugin_config_without_volatile_fields_requires_restart() -> None:
    app = await _boot((PluginRow(id="plain", plugin=f"{DUMMY}:NonVolatilePlugin", config={"threshold": 1}),))
    try:
        kernel = app.service("kernel")
        assert kernel.apply_plugin_config("plain", {"threshold": 3}) is False
        assert app.restart_required == {"plain"}
        states = {state["id"]: state for state in app.row_states()}
        assert states["plain"]["state"] == "restart-required"
    finally:
        await app.stop()


async def test_apply_plugin_config_unknown_plugin_returns_false() -> None:
    app = await _boot((PluginRow(id="vol", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 2}),))
    try:
        kernel = app.service("kernel")
        assert kernel.apply_plugin_config("nope", {"threshold": 4}) is False
        assert app.restart_required == frozenset()
    finally:
        await app.stop()


async def test_apply_plugin_config_rejects_invalid_value_without_restart() -> None:
    app = await _boot((PluginRow(id="vol", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 2}),))
    try:
        kernel = app.service("kernel")
        assert kernel.apply_plugin_config("vol", {"threshold": "not-an-int"}) is False
        assert app.restart_required == frozenset()
        states = {state["id"]: state for state in app.row_states()}
        assert states["vol"]["state"] == "active"
        assert app.mount_record("vol").instance.config.threshold == 2
    finally:
        await app.stop()


async def test_apply_plugin_config_same_values_is_applied_and_clears_restart() -> None:
    app = await _boot((PluginRow(id="vol", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 2}),))
    try:
        kernel = app.service("kernel")
        assert kernel.apply_plugin_config("vol", {"label": "other"}) is False
        assert app.restart_required == {"vol"}
        assert kernel.apply_plugin_config("vol", {"threshold": 2}) is True
        assert app.restart_required == frozenset()
    finally:
        await app.stop()


async def test_mounted_plugins_lists_disabled_rows() -> None:
    app = await _boot(
        (
            PluginRow(id="off", plugin=f"{DUMMY}:VolatilePlugin", disabled=True),
            PluginRow(id="vol", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 2}),
        )
    )
    try:
        kernel = app.service("kernel")
        mounted = {plugin["id"]: plugin for plugin in kernel.mounted_plugins()}
        assert mounted["off"]["state"] == "disabled"
        assert mounted["off"]["disabled"] is True
        assert mounted["vol"]["state"] == "active"
    finally:
        await app.stop()


async def test_apply_plugin_config_rejects_group_rows() -> None:
    app = await _boot(
        (
            PluginRow(
                id="group",
                group=True,
                children=(PluginRow(id="child", plugin=f"{DUMMY}:VolatilePlugin", config={"threshold": 1}),),
            ),
        )
    )
    try:
        kernel = app.service("kernel")
        assert kernel.apply_plugin_config("group", {"threshold": 5}) is False
    finally:
        await app.stop()


def test_unattached_runtime_has_no_plugins() -> None:
    runtime = object.__new__(KernelRuntime)
    runtime.__dict__["_app"] = None
    assert runtime.mounted_plugins() == []
    assert runtime.apply_plugin_config("anything", {"threshold": 1}) is False
