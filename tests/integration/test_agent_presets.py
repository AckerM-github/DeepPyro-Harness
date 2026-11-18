"""Acceptance: agent presets scope tools and prompt sections per session.

An out-of-tree bundle registers a preset plus a scoped tool and a scoped prompt
section; agents created with that preset see them, everyone else does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.agent.native_factory import AgentBuild, PromptFacts
from deeppyro.api import DEFAULT_PRESET_ID, PresetPluginRow, PresetSpec
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import DeeppyroError
from deeppyro.kernel import boot
from deeppyro.prompt import PromptContext
from deeppyro.session.fold import fold
from deeppyro.session.native_control import NewSession
from deeppyro.session.schema import NativeSession

PLUGIN_SOURCE = (
    "from deeppyro.api import Plugin, PresetSpec\n"
    "from deeppyro.core.protocols import ToolResult\n"
    "from deeppyro.tools.base import BaseTool, make_parameters\n"
    "\n"
    "\n"
    "class ReviewTool(BaseTool):\n"
    '    name = "review_note"\n'
    '    description = "Record one review note."\n'
    '    parameters = make_parameters({"note": {"type": "string"}}, required=["note"])\n'
    "\n"
    "    async def run(self, args, ctx):\n"
    '        return ToolResult(text="noted: " + str(args.get("note", "")))\n'
    "\n"
    "\n"
    "class SharedTool(BaseTool):\n"
    '    name = "shared_ping"\n'
    '    description = "Available to every preset."\n'
    "    parameters = make_parameters({})\n"
    "\n"
    "    async def run(self, args, ctx):\n"
    '        return ToolResult(text="pong")\n'
    "\n"
    "\n"
    "class PresetPlugin(Plugin):\n"
    '    plugin_id = "preset-bundle"\n'
    '    version = "0.1.0"\n'
    '    inject = ["tools", "prompt", "presets"]\n'
    "\n"
    "    def setup(self):\n"
    "        self.ctx.register_preset(PresetSpec(\n"
    '            id="reviewer",\n'
    '            name="Reviewer",\n'
    '            description="read-only review mode",\n'
    '            model="reviewer-model",\n'
    '            instructions=("Review only.",),\n'
    "            order=20,\n"
    "        ))\n"
    "        self.ctx.register_tool(lambda config, workspace: SharedTool())\n"
    '        self.ctx.register_tool(lambda config, workspace: ReviewTool(), scope="reviewer")\n'
    "        self.ctx.register_prompt_section(\n"
    '            "review-hint",\n'
    "            45,\n"
    '            lambda tools, prompt_ctx: "## Review mode\\nDo not edit files.",\n'
    '            scope="reviewer",\n'
    "        )\n"
)
BUNDLE_TOML = (
    '[bundle]\nid = "preset-bundle"\nname = "Preset bundle"\nversion = "0.1.0"\norder = 50\n'
    "\n"
    '[[plugins]]\nid = "preset-bundle"\nplugin = "preset_plugin.py:PresetPlugin"\n'
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep user bundles, manifests and sessions out of the test run."""
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(base_url="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])
        },
        agent={"provider": "local", "model": "m"},
    )


def _facts(tmp_path: Path) -> PromptFacts:
    return PromptFacts(
        cwd=tmp_path,
        model="m",
        permission_preset="workspace-write",
        approval_policy="ask",
    )


def _prompt_context(tmp_path: Path) -> PromptContext:
    return PromptContext(
        cwd=tmp_path,
        platform="test",
        session_id="session-test",
        started_at=0,
        permission_preset="workspace-write",
        approval_policy="ask",
        model="m",
    )


def _system_prompt(session: NativeSession) -> str:
    view = fold(session.header, session.events)
    assert view.system is not None
    return view.system.text()


async def test_preset_scopes_tools_and_prompt_sections(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "preset"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(BUNDLE_TOML, encoding="utf-8")
    (bundle_dir / "preset_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    app = await boot(_config(tmp_path), bundle_paths=[str(bundle_dir)])
    try:
        presets = app.require(PresetsPlugin)
        ids = presets.ids()
        assert "standard" in ids
        assert "reviewer" in ids
        reviewer = presets.resolve("reviewer")
        assert reviewer.instructions == ("Review only.",)
        assert reviewer.model == "reviewer-model"
        with pytest.raises(DeeppyroError):
            presets.resolve("nope")

        tools = app.require(ToolsPlugin)
        default_tools = tools.registry_for(tmp_path)
        assert "shared_ping" in default_tools
        assert "review_note" not in default_tools
        scoped_tools = tools.registry_for(tmp_path, scope="reviewer")
        assert "shared_ping" in scoped_tools
        assert "review_note" in scoped_tools
        result = await scoped_tools.get("review_note").run({"note": "ok"}, None)
        assert result.text == "noted: ok"

        prompt_service = app.require(PromptPlugin)
        prompt_context = _prompt_context(tmp_path)
        default_prompt = prompt_service.build([], prompt_context)
        assert "## Review mode" not in default_prompt
        assert "## Operating rules" in default_prompt
        scoped_prompt = prompt_service.build([], prompt_context, scope="reviewer")
        assert "## Review mode" in scoped_prompt
        assert "Do not edit files." in scoped_prompt
    finally:
        await app.stop()


async def test_agents_apply_the_selected_preset(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "preset"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(BUNDLE_TOML, encoding="utf-8")
    (bundle_dir / "preset_plugin.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    app = await boot(_config(tmp_path), bundle_paths=[str(bundle_dir)])
    try:
        presets = app.require(PresetsPlugin)
        reviewer = presets.resolve("reviewer")
        assert reviewer.instructions == ("Review only.",)
        assert reviewer.model == "reviewer-model"

        sessions = app.require(SessionsPlugin)
        agents = app.require(AgentsPlugin)
        session = sessions.native_create(NewSession(cwd=tmp_path, preset="reviewer"))
        assert session.header.preset == "reviewer"
        try:
            # An empty build model falls back to the preset's model override.
            host = agents.host(
                session,
                AgentBuild(
                    provider="local",
                    model="",
                    cwd=tmp_path,
                    preset="reviewer",
                    prompt_facts=_facts(tmp_path),
                ),
            )
            try:
                assert host._loop._model == "reviewer-model"
                names = {tool.name for tool in host._tools}
                assert "review_note" in names
                assert "shared_ping" in names
                prompt = _system_prompt(session)
                assert "## Review mode" in prompt
                assert "Do not edit files." in prompt
                assert "Review only." in prompt
            finally:
                await host.aclose()
        finally:
            session.close()

        default_session = sessions.native_create(NewSession(cwd=tmp_path))
        assert default_session.header.preset == DEFAULT_PRESET_ID
        try:
            assert presets.resolve(DEFAULT_PRESET_ID).instructions == ()
            default_host = agents.host(
                default_session,
                AgentBuild(
                    provider="local",
                    model="",
                    cwd=tmp_path,
                    preset=DEFAULT_PRESET_ID,
                    prompt_facts=_facts(tmp_path),
                ),
            )
            try:
                default_prompt = _system_prompt(default_session)
                assert "## Review mode" not in default_prompt
                assert "Review only." not in default_prompt
                assert "review_note" not in {tool.name for tool in default_host._tools}
            finally:
                await default_host.aclose()
        finally:
            default_session.close()
    finally:
        await app.stop()


REALM_PLUGIN_SOURCE = 'from deeppyro.api import Plugin\nfrom deeppyro.core.protocols import ToolResult\nfrom deeppyro.tools.base import BaseTool, make_parameters\n\n\nclass RealmTool(BaseTool):\n    name = "realm_note"\n    description = "Tool delivered by the preset realm."\n    parameters = make_parameters({"note": {"type": "string"}}, required=["note"])\n\n    async def run(self, args, ctx):\n        return ToolResult(text="realm: " + str(args.get("note", "")))\n\n\nclass RealmPlugin(Plugin):\n    plugin_id = "writer-realm"\n    version = "0.1.0"\n    inject = ["tools", "prompt"]\n\n    def setup(self):\n        self.ctx.register_tool(lambda config, workspace: RealmTool())\n        self.ctx.register_prompt_section("realm-hint", 60, lambda tools, prompt_ctx: "## Realm mode\\nRealm tools only.")\n'
REALM_PLUGIN_V2_SOURCE = 'from deeppyro.api import Plugin\nfrom deeppyro.core.protocols import ToolResult\nfrom deeppyro.tools.base import BaseTool, make_parameters\n\n\nclass RealmToolV2(BaseTool):\n    name = "realm_note_v2"\n    description = "Tool delivered by the preset realm."\n    parameters = make_parameters({"note": {"type": "string"}}, required=["note"])\n\n    async def run(self, args, ctx):\n        return ToolResult(text="realm: " + str(args.get("note", "")))\n\n\nclass RealmPluginV2(Plugin):\n    plugin_id = "writer-realm"\n    version = "0.1.0"\n    inject = ["tools", "prompt"]\n\n    def setup(self):\n        self.ctx.register_tool(lambda config, workspace: RealmToolV2())\n        self.ctx.register_prompt_section("realm-hint-v2", 60, lambda tools, prompt_ctx: "## Realm mode v2\\nRealm tools only.")\n'
REALM_PRESET_SOURCE = 'from pathlib import Path\n\nfrom deeppyro.api import Plugin, PresetPluginRow, PresetSpec\n\n\nclass RealmPresetPlugin(Plugin):\n    plugin_id = "realm-preset-bundle"\n    version = "0.1.0"\n    inject = ["presets"]\n\n    def setup(self):\n        base = Path(__file__).resolve().parent\n        self.ctx.register_preset(PresetSpec(\n            id="realm-writer",\n            name="Realm Writer",\n            model="realm-model",\n            instructions=("Use the realm.",),\n            order=25,\n            plugins=(\n                PresetPluginRow(id="writer-realm", plugin="realm_plugin.py:RealmPlugin"),\n            ),\n            base=base,\n        ))\n'
REALM_BUNDLE_TOML = '[bundle]\nid = "realm-preset-bundle"\nname = "Realm preset bundle"\nversion = "0.1.0"\norder = 51\n\n[[plugins]]\nid = "realm-preset-bundle"\nplugin = "preset_plugin.py:RealmPresetPlugin"\n'


async def test_preset_realm_activates_and_live_agent_keeps_its_revision(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "realm-preset"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(REALM_BUNDLE_TOML, encoding="utf-8")
    (bundle_dir / "preset_plugin.py").write_text(REALM_PRESET_SOURCE, encoding="utf-8")
    (bundle_dir / "realm_plugin.py").write_text(REALM_PLUGIN_SOURCE, encoding="utf-8")
    app = await boot(_config(tmp_path), bundle_paths=[str(bundle_dir)])
    try:
        presets = app.require(PresetsPlugin)
        # Realms are materialized eagerly by the registry; hosts await them
        # before creating agents so realm contributions are visible.
        await presets.ensure_all()
        writer = presets.resolve("realm-writer")
        assert writer.revision == presets.revision_of("realm-writer")
        assert presets.realm_failures("realm-writer") == []

        sessions = app.require(SessionsPlugin)
        agents = app.require(AgentsPlugin)
        session = sessions.native_create(NewSession(cwd=tmp_path, preset="realm-writer"))
        assert session.header.preset == "realm-writer"
        try:
            host = agents.host(
                session,
                AgentBuild(
                    provider="local",
                    model="",
                    cwd=tmp_path,
                    preset="realm-writer",
                    prompt_facts=_facts(tmp_path),
                ),
            )
            try:
                assert host._loop._model == "realm-model"
                names = {tool.name for tool in host._tools}
                assert "realm_note" in names
                assert "realm_note_v2" not in names
                assert "## Realm mode" in _system_prompt(session)
            finally:
                await host.aclose()
        finally:
            session.close()

        # Editing the preset creates a new immutable revision; the live agent
        # above keeps the snapshot it was created with.
        (bundle_dir / "realm_plugin_v2.py").write_text(REALM_PLUGIN_V2_SOURCE, encoding="utf-8")
        presets.add(
            PresetSpec(
                id="realm-writer",
                name="Realm Writer",
                model="realm-model",
                instructions=("Use the realm.",),
                order=25,
                plugins=(PresetPluginRow(id="writer-realm", plugin="realm_plugin_v2.py:RealmPluginV2"),),
                base=bundle_dir,
            )
        )
        await presets.ensure("realm-writer")
        new_revision = presets.revision_of("realm-writer")
        assert new_revision == writer.revision + 1

        new_session = sessions.native_create(NewSession(cwd=tmp_path, preset="realm-writer"))
        try:
            new_host = agents.host(
                new_session,
                AgentBuild(
                    provider="local",
                    model="",
                    cwd=tmp_path,
                    preset="realm-writer",
                    prompt_facts=_facts(tmp_path),
                ),
            )
            try:
                assert presets.resolve("realm-writer").revision == new_revision
                new_names = {tool.name for tool in new_host._tools}
                assert "realm_note_v2" in new_names
                assert "realm_note" not in new_names
            finally:
                await new_host.aclose()
        finally:
            new_session.close()
    finally:
        await app.stop()
