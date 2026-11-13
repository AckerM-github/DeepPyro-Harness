"""Acceptance: an out-of-tree bundle contributes every extension point.

The bundle lives in a temporary directory and is never imported into the repo,
so this is the "install one plugin without touching our source" contract.
"""

from __future__ import annotations

from pathlib import Path

from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.kernel import boot
from deeppyro.prompt import PromptContext

PLUGIN_SOURCE = 'from deeppyro.api import Plugin\nfrom deeppyro.core.protocols import ToolResult\nfrom deeppyro.tools.base import BaseTool, make_parameters\n\n\nclass GreetTool(BaseTool):\n    name = "greet"\n    description = "Greet somebody by name."\n    parameters = make_parameters({"who": {"type": "string"}}, required=["who"])\n\n    async def run(self, args, ctx):\n        return ToolResult(text="hello " + str(args.get("who", "world")))\n\n\nclass GreetPlugin(Plugin):\n    plugin_id = "greet"\n    version = "0.1.0"\n    inject = ["tools", "prompt", "commands", "llm"]\n\n    def setup(self, ctx, config):\n        ctx.register_tool(lambda config, workspace: GreetTool())\n        ctx.register_prompt_section("greet-hint", 45, lambda tools, prompt_ctx: "## Greeting\\nAlways be polite.")\n        ctx.register_command("greet", self.run_greet, summary="greet someone", usage="/greet [name]")\n        ctx.register_provider("fake", lambda llm: object())\n\n    async def run_greet(self, host, argument):\n        host.greeting = argument or "world"\n'
BUNDLE_TOML = '[bundle]\nid = "greet-bundle"\nname = "Greet bundle"\nversion = "0.1.0"\norder = 50\n\n[[plugins]]\nid = "greet"\nplugin = "greet_plugin.py:GreetPlugin"\n'


def _config() -> Config:
    return Config(
        providers={"local": ProviderConfig(baseURL="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])},
        agent={"provider": "local", "model": "m"},
    )


async def test_external_bundle_contributes_every_extension(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "greet"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(BUNDLE_TOML, encoding="utf-8")
    (bundle_dir / "greet_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    app = await boot(_config(), bundle_paths=[str(bundle_dir)])
    try:
        tools = app.service("tools").registry_for(tmp_path)
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
        prompt = app.service("prompt").build([], prompt_context)
        assert "## Greeting" in prompt

        assert "greet" in app.service("commands").names()
        assert app.service("llm").provider("fake") is not None
        assert app.started["greet"] == "ACTIVE"
    finally:
        await app.stop()
