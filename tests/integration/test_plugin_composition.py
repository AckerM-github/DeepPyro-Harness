"""Acceptance: an out-of-tree bundle contributes every extension point.

The bundle lives in a temporary directory and is never imported into the repo,
so this is the "install one plugin without touching our source" contract.
"""

from __future__ import annotations

from pathlib import Path

from deeppyro.builtin.commands import CommandsPlugin
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot
from deeppyro.prompt import PromptContext

PLUGIN_SOURCE = 'from deeppyro.api import Plugin\nfrom deeppyro.core.protocols import ToolResult\nfrom deeppyro.tools.base import BaseTool, make_parameters\n\n\nclass GreetTool(BaseTool):\n    name = "greet"\n    description = "Greet somebody by name."\n    parameters = make_parameters({"who": {"type": "string"}}, required=["who"])\n\n    async def run(self, args, ctx):\n        return ToolResult(text="hello " + str(args.get("who", "world")))\n\n\nclass GreetPlugin(Plugin):\n    plugin_id = "greet"\n    version = "0.1.0"\n    inject = ["tools", "prompt", "commands", "llm"]\n\n    def setup(self):\n        self.ctx.register_tool(lambda config, workspace: GreetTool())\n        self.ctx.register_prompt_section("greet-hint", 45, lambda tools, prompt_ctx: "## Greeting\\nAlways be polite.")\n        self.ctx.register_command("greet", self.run_greet, summary="greet someone", usage="/greet [name]")\n        self.ctx.register_provider("fake", lambda llm: object())\n\n    async def run_greet(self, host, argument):\n        host.greeting = argument or "world"\n'
BUNDLE_TOML = '[bundle]\nid = "greet-bundle"\nname = "Greet bundle"\nversion = "0.1.0"\norder = 50\n\n[[plugins]]\nid = "greet"\nplugin = "greet_plugin.py:GreetPlugin"\n'


def _config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(base_url="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])
        },
        agent={"provider": "local", "model": "m"},
    )


async def test_external_bundle_contributes_every_extension(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "greet"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(BUNDLE_TOML, encoding="utf-8")
    (bundle_dir / "greet_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    app = await boot(_config(), bundle_paths=[str(bundle_dir)])
    try:
        tools = app.require(ToolsPlugin).registry_for(tmp_path)
        assert "greet" in tools
        result = await tools.get("greet").run({"who": "pyro"}, None)
        assert result.text == "hello pyro"

        prompt_context = PromptContext(
            cwd=tmp_path,
            platform="test",
            session_id="session-test",
            started_at=0,
            permission_preset="workspace-write",
            approval_policy="ask",
            model="m",
        )
        prompt = app.require(PromptPlugin).build([], prompt_context)
        assert "## Greeting" in prompt

        assert "greet" in app.require(CommandsPlugin).names()
        assert app.require(LlmPlugin).provider("fake") is not None
        assert app.started["greet"] == "ACTIVE"
    finally:
        await app.stop()


V2_PLUGIN_SOURCE = """from deeppyro.api import Plugin
from deeppyro.builtin.commands import CommandsPlugin
from deeppyro.core.protocols import ToolResult
from deeppyro.tools.base import BaseTool, make_parameters


class StampTool(BaseTool):
    name = "stamp"
    description = "Stamp a value."
    parameters = make_parameters({"value": {"type": "string"}}, required=["value"])

    async def run(self, args, ctx):
        return ToolResult(text="stamped " + str(args.get("value", "")))


class StampPlugin(Plugin):
    plugin_id = "stamp"
    version = "2.0.0"
    api_version = 2
    inject = (CommandsPlugin,)

    def setup(self):
        self.ctx.register_tool(lambda config, workspace: StampTool())
"""
V2_BUNDLE_TOML = '[bundle]\nid = "stamp-bundle"\nname = "Stamp bundle"\nversion = "2.0.0"\norder = 51\n\n[[plugins]]\nid = "stamp"\nplugin = "stamp_plugin.py:StampPlugin"\n'


async def test_v2_plugin_is_rejected_with_a_migration_hint(tmp_path: Path) -> None:
    """The v1/v2 plugin API is a hard cut: a stale plugin fails to mount."""
    bundle_dir = tmp_path / "stamp"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(V2_BUNDLE_TOML, encoding="utf-8")
    (bundle_dir / "stamp_plugin.py").write_text(V2_PLUGIN_SOURCE, encoding="utf-8")
    app = await boot(_config(), bundle_paths=[str(bundle_dir)])
    try:
        failure = next(status for status in app.failures if status.id == "stamp")
        assert failure.state == "failed"
        assert "stamp" not in app.require(ToolsPlugin).registry_for(tmp_path)
        assert "api_version 2" in failure.error
        assert "no compatibility shim" in failure.error
        assert "setup(self)" in failure.error
    finally:
        await app.stop()
