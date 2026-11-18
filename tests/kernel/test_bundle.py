"""Bundle-layer tests: default plugin order, aliases, patch manifests, boot."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import (
    DEFAULT_PLUGINS,
    PluginSpec,
    boot,
    bundle_specs,
    create_app,
)

EXPECTED_ORDER = [
    "kernel",
    "llm",
    "sessions",
    "sandbox",
    "tools",
    "goal",
    "goal-round-driver",
    "tools-builtin",
    "mcp",
    "compress",
    "schedule",
    "working-directory",
    "user-questions",
    "attachments",
    "hooks",
    "hook-observability",
    "session-title",
    "jobs",
    "plugin-tools",
    "presets",
    "prompt",
    "prompt-core",
    "prompt-extension",
    "approval-policy",
    "ptc",
    "agent-presets",
    "memory",
    "usage",
    "telemetry",
    "commands",
    "commands-builtin",
    "skills",
    "agents",
    "team",
    "subagent",
    "task",
    "activity",
    "slots",
    "plugin-manager",
    "settings",
    "inspect",
]


def _config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


def test_default_plugin_order() -> None:
    assert [spec.id for spec in DEFAULT_PLUGINS] == EXPECTED_ORDER
    # Core bundle rows point at concrete plugin classes, not aliases.
    assert all(spec.name and ":" in spec.name for spec in DEFAULT_PLUGINS)
    assert all(spec.enabled() for spec in DEFAULT_PLUGINS)


def test_every_bundle_alias_resolves_to_a_class() -> None:
    for spec in DEFAULT_PLUGINS:
        cls = spec.resolve()
        assert isinstance(cls, type), spec.id


def test_bundle_specs_applies_patch_file(tmp_path: Path) -> None:
    manifest = tmp_path / "plugins.toml"
    manifest.write_text(
        """
[[plugins]]
id = "llm"
config = { timeout_s = 42 }

[[plugins]]
id = "agents"
disabled = true

[[plugins]]
id = "echo"
name = "tests.kernel.plugins_dummy:CounterPlugin"
after = "llm"
config = { start = 5 }
""",
        encoding="utf-8",
    )
    specs = bundle_specs([str(manifest)])
    by_id = {spec.id: spec for spec in specs}
    assert by_id["llm"].config == {"timeout_s": 42}
    assert by_id["agents"].disabled is True
    assert [spec.id for spec in specs] == [
        "kernel",
        "llm",
        "echo",
        "sessions",
        "sandbox",
        "tools",
        "goal",
        "goal-round-driver",
        "tools-builtin",
        "mcp",
        "compress",
        "schedule",
        "working-directory",
        "user-questions",
        "attachments",
        "hooks",
        "hook-observability",
        "session-title",
        "jobs",
        "plugin-tools",
        "presets",
        "prompt",
        "prompt-core",
        "prompt-extension",
        "approval-policy",
        "ptc",
        "agent-presets",
        "memory",
        "usage",
        "telemetry",
        "commands",
        "commands-builtin",
        "skills",
        "agents",
        "team",
        "subagent",
        "task",
        "activity",
        "slots",
        "plugin-manager",
        "settings",
        "inspect",
    ]


async def test_boot_skips_disabled_plugins(tmp_path: Path) -> None:
    manifest = tmp_path / "plugins.toml"
    manifest.write_text(
        '[[plugins]]\nid = "sessions"\ndisabled = true\n'
        '[[plugins]]\nid = "sandbox"\ndisabled = true\n'
        '[[plugins]]\nid = "tools"\ndisabled = true\n'
        '[[plugins]]\nid = "agents"\ndisabled = true\n',
        encoding="utf-8",
    )
    app = await boot(_config(), manifest_paths=[str(manifest)])
    try:
        assert app.get(LlmPlugin) is not None
        assert app.get(SessionsPlugin) is None
        assert app.get(ToolsPlugin) is None
        # Every loaded row is listed in bundle order; disabled rows are absent.
        assert list(app.started) == [
            "kernel",
            "llm",
            "goal",
            "goal-round-driver",
            "tools-builtin",
            "mcp",
            "compress",
            "schedule",
            "working-directory",
            "user-questions",
            "attachments",
            "hooks",
            "hook-observability",
            "session-title",
            "jobs",
            "plugin-tools",
            "presets",
            "prompt",
            "prompt-core",
            "prompt-extension",
            "approval-policy",
            "ptc",
            "agent-presets",
            "memory",
            "usage",
            "telemetry",
            "commands",
            "commands-builtin",
            "skills",
            "team",
            "subagent",
            "task",
            "activity",
            "slots",
            "plugin-manager",
            "settings",
            "inspect",
        ]
        # tools-builtin, working-directory, jobs and
        # plugin-tools inject the tools service, so they wait too.
        assert app.started["tools-builtin"] == "PENDING"
        assert app.started["ptc"] == "PENDING"
        assert app.started["compress"] == "PENDING"
        assert app.started["schedule"] == "PENDING"
        assert app.started["working-directory"] == "PENDING"
        assert app.started["jobs"] == "PENDING"
        assert app.started["plugin-tools"] == "PENDING"
        assert app.started["team"] == "PENDING"
        assert app.started["attachments"] == "ACTIVE"
        assert app.started["memory"] == "ACTIVE"
        assert app.started["llm"] == "ACTIVE"
        assert app.started["hooks"] == "ACTIVE"
        assert app.started["hook-observability"] == "ACTIVE"
        assert app.started["presets"] == "ACTIVE"
        assert app.started["prompt"] == "ACTIVE"
        assert app.started["prompt-core"] == "ACTIVE"
        assert app.started["prompt-extension"] == "ACTIVE"
        assert app.started["approval-policy"] == "ACTIVE"
        assert app.started["agent-presets"] == "ACTIVE"
        assert app.started["commands"] == "ACTIVE"
        assert app.started["commands-builtin"] == "ACTIVE"
    finally:
        await app.stop()
    assert app.get(LlmPlugin) is None


async def test_boot_fails_with_missing_domain_module() -> None:
    from deeppyro.kernel.app import KernelBootError

    specs = [PluginSpec(id="broken", name="tests.kernel.plugins_dummy:ExplodingPlugin")]
    with pytest.raises(KernelBootError) as excinfo:
        await create_app(_config(), plugins=specs)
    assert "broken" in str(excinfo.value)
