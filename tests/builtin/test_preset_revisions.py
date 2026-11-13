"""Preset revisions and per-preset plugin realms (Layer D).

Acceptance for DSH-style per-session composition: a preset registration is an
immutable revision, plugin rows carried by a preset are materialized once in a
child realm, contributions are pinned to (preset, revision), and a live agent
keeps seeing the revision it was created with.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    SUBAGENT_TOOL_NAMES,
    TEAM_PRESET_ID,
    TEAM_TOOL_NAMES,
    PluginDependencyError,
    PresetPluginRow,
    PresetSpec,
)
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.errors import DeeppyroError
from deeppyro.kernel.app import App
from deeppyro.kernel.bundle import boot
from deeppyro.prompt import PromptContext
from deeppyro.tools.base import BaseTool
from deeppyro.tools.registry import ToolRegistry

BUNDLE_TOML = (
    '[bundle]\nid = "{bundle_id}"\nname = "Preset revisions"\nversion = "0.1.0"\norder = 50\n'
    "\n"
    '[[plugins]]\nid = "{plugin_id}"\nplugin = "preset_plugin.py:{plugin_class}"\n'
)

REALM_PLUGIN = (
    "from deeppyro.api import Plugin\n"
    "from deeppyro.core.protocols import ToolResult\n"
    "from deeppyro.tools.base import BaseTool, make_parameters\n"
    "\n"
    "\n"
    "class RealmNoteTool(BaseTool):\n"
    '    name = "realm_note"\n'
    '    description = "Tool mounted by the preset realm."\n'
    '    parameters = make_parameters({"note": {"type": "string"}})\n'
    "\n"
    "    async def run(self, args, ctx):\n"
    '        return ToolResult(text="realm: " + str(args.get("note", "")))\n'
    "\n"
    "\n"
    "class RealmPlugin(Plugin):\n"
    '    plugin_id = "realm"\n'
    '    inject = ["tools", "prompt"]\n'
    "\n"
    "    def setup(self, ctx, config):\n"
    "        ctx.register_tool(lambda config, workspace: RealmNoteTool())\n"
    "        ctx.register_prompt_section(\n"
    '            "realm-hint", 60, lambda tools, prompt_ctx: "## Realm hint\\nRealm section."\n'
    "        )\n"
)

REALM_PLUGIN_V2 = (
    "from deeppyro.api import Plugin\n"
    "from deeppyro.core.protocols import ToolResult\n"
    "from deeppyro.tools.base import BaseTool, make_parameters\n"
    "\n"
    "\n"
    "class RealmNoteToolV2(BaseTool):\n"
    '    name = "realm_note_v2"\n'
    '    description = "Second-generation realm tool."\n'
    "    parameters = make_parameters({})\n"
    "\n"
    "    async def run(self, args, ctx):\n"
    '        return ToolResult(text="realm-v2")\n'
    "\n"
    "\n"
    "class RealmPlugin(Plugin):\n"
    '    plugin_id = "realm-v2"\n'
    '    inject = ["tools", "prompt"]\n'
    "\n"
    "    def setup(self, ctx, config):\n"
    "        ctx.register_tool(lambda config, workspace: RealmNoteToolV2())\n"
    "        ctx.register_prompt_section(\n"
    '            "realm-hint-v2", 61, lambda tools, prompt_ctx: "## Realm v2\\nNew section."\n'
    "        )\n"
)

PRESET_PLUGIN = (
    "from pathlib import Path\n"
    "\n"
    "from deeppyro.api import Plugin, PresetPluginRow, PresetSpec\n"
    "from deeppyro.core.protocols import ToolResult\n"
    "from deeppyro.tools.base import BaseTool, make_parameters\n"
    "\n"
    "BASE = Path(__file__).parent\n"
    "\n"
    "\n"
    "class WriterTool(BaseTool):\n"
    '    name = "writer_note"\n'
    '    description = "Legacy scoped tool without a revision."\n'
    '    parameters = make_parameters({"text": {"type": "string"}})\n'
    "\n"
    "    async def run(self, args, ctx):\n"
    '        return ToolResult(text="writer: " + str(args.get("text", "")))\n'
    "\n"
    "\n"
    "class PresetPlugin(Plugin):\n"
    '    plugin_id = "writer-preset"\n'
    '    version = "0.1.0"\n'
    '    inject = ["tools", "prompt", "presets"]\n'
    "\n"
    "    def setup(self, ctx, config):\n"
    "        ctx.register_preset(PresetSpec(\n"
    '            id="writer",\n'
    '            name="Writer",\n'
    '            description="writing mode with its own plugin realm",\n'
    '            model="writer-model",\n'
    '            instructions=("Write clearly.",),\n'
    "            order=30,\n"
    "            plugins=(\n"
    '                PresetPluginRow(id="writer-realm", plugin="realm_plugin.py:RealmPlugin"),\n'
    "            ),\n"
    "            base=BASE,\n"
    "        ))\n"
    '        ctx.register_tool(lambda config, workspace: WriterTool(), scope="writer")\n'
    "        ctx.register_prompt_section(\n"
    '            "writer-hint",\n'
    "            40,\n"
    '            lambda tools, prompt_ctx: "## Writer mode\\nUse short sentences.",\n'
    '            scope="writer",\n'
    "        )\n"
)

COLLISION_PLUGIN = (
    "from pathlib import Path\n"
    "\n"
    "from deeppyro.api import Plugin, PresetPluginRow, PresetSpec\n"
    "from deeppyro.core.protocols import ToolResult\n"
    "from deeppyro.tools.base import BaseTool, make_parameters\n"
    "\n"
    "BASE = Path(__file__).parent\n"
    "\n"
    "\n"
    "class HostSharedTool(BaseTool):\n"
    '    name = "shared_host"\n'
    '    description = "Host tool that a realm also uses."\n'
    "    parameters = make_parameters({})\n"
    "\n"
    "    async def run(self, args, ctx):\n"
    '        return ToolResult(text="host")\n'
    "\n"
    "\n"
    "class PresetPlugin(Plugin):\n"
    '    plugin_id = "collision-preset"\n'
    '    inject = ["tools", "presets"]\n'
    "\n"
    "    def setup(self, ctx, config):\n"
    "        ctx.register_tool(lambda config, workspace: HostSharedTool())\n"
    "        ctx.register_preset(PresetSpec(\n"
    '            id="collide",\n'
    '            name="Collide",\n'
    "            order=40,\n"
    "            plugins=(\n"
    '                PresetPluginRow(id="collide-realm", plugin="realm_plugin.py:RealmPlugin"),\n'
    "            ),\n"
    "            base=BASE,\n"
    "        ))\n"
)

COLLIDING_REALM_PLUGIN = (
    "from deeppyro.api import Plugin\n"
    "from deeppyro.core.protocols import ToolResult\n"
    "from deeppyro.tools.base import BaseTool, make_parameters\n"
    "\n"
    "\n"
    "class RealmSharedTool(BaseTool):\n"
    '    name = "shared_host"\n'
    '    description = "Realm tool with the same name as a host tool."\n'
    "    parameters = make_parameters({})\n"
    "\n"
    "    async def run(self, args, ctx):\n"
    '        return ToolResult(text="realm")\n'
    "\n"
    "\n"
    "class RealmPlugin(Plugin):\n"
    '    plugin_id = "collide-realm"\n'
    '    inject = ["tools"]\n'
    "\n"
    "    def setup(self, ctx, config):\n"
    "        ctx.register_tool(lambda config, workspace: RealmSharedTool())\n"
)

BROKEN_PLUGIN = (
    "from pathlib import Path\n"
    "\n"
    "from deeppyro.api import Plugin, PresetPluginRow, PresetSpec\n"
    "\n"
    "BASE = Path(__file__).parent\n"
    "\n"
    "\n"
    "class PresetPlugin(Plugin):\n"
    '    plugin_id = "broken-preset"\n'
    '    inject = ["presets"]\n'
    "\n"
    "    def setup(self, ctx, config):\n"
    "        ctx.register_preset(PresetSpec(\n"
    '            id="broken",\n'
    '            name="Broken",\n'
    "            order=70,\n"
    "            plugins=(\n"
    '                PresetPluginRow(id="bad-row", plugin="missing_plugin.py:MissingPlugin"),\n'
    '                PresetPluginRow(id="good-row", plugin="realm_plugin.py:RealmPlugin"),\n'
    "            ),\n"
    "            base=BASE,\n"
    "        ))\n"
)


class DummySink:
    def emit(self, event: dict[str, Any]) -> None:
        pass


def _named_tool(name: str) -> Any:
    tool = BaseTool()
    tool.name = name  # type: ignore[misc]
    return tool


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={"local": ProviderConfig(baseURL="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])},
        agent={"provider": "local", "model": "m"},
        sessionDir=tmp_path / "sessions",
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


def _write_bundle(
    tmp_path: Path,
    name: str,
    plugin_source: str,
    files: dict[str, str],
) -> Path:
    bundle_dir = tmp_path / name
    bundle_dir.mkdir()
    toml = BUNDLE_TOML.format(bundle_id=name, plugin_id=f"{name}-plugin", plugin_class="PresetPlugin")
    (bundle_dir / "bundle.toml").write_text(toml, encoding="utf-8")
    (bundle_dir / "preset_plugin.py").write_text(plugin_source, encoding="utf-8")
    for filename, source in files.items():
        (bundle_dir / filename).write_text(source, encoding="utf-8")
    return bundle_dir


async def _boot(tmp_path: Path, bundle_dir: Path) -> App:
    return await boot(_config(tmp_path), bundle_paths=[str(bundle_dir)])


def _make_agent(app: App, tmp_path: Path, preset: str | None = None) -> Any:
    sessions = app.service("sessions")
    session = sessions.create(str(tmp_path), agent_preset=preset)
    agents = app.service("agents")
    return agents.create(
        session=session,
        provider=object(),
        options=agents.options(),
        sink=DummySink(),
        cwd=str(tmp_path),
        preset=preset,
    )


async def test_preset_revisions_are_immutable(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "writer",
        PRESET_PLUGIN,
        {"realm_plugin.py": REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        presets = app.service("presets")
        assert "default" in presets.ids()
        assert "writer" in presets.ids()
        assert presets.revisions("writer") == [1]
        first = presets.resolve("writer")
        assert first.revision == 1
        assert first.instructions == ("Write clearly.",)
        assert first.model == "writer-model"
        assert presets.revision_of("writer") == 1
        assert presets.revision_of("missing") is None
        assert presets.resolve_revision("writer", 1) == first

        (bundle_dir / "realm_plugin_v2.py").write_text(REALM_PLUGIN_V2, encoding="utf-8")
        presets.add(
            PresetSpec(
                id="writer",
                name="Writer",
                description="second revision",
                model="writer-model-v2",
                instructions=("Write nicer.",),
                order=30,
                plugins=(
                    PresetPluginRow(
                        id="writer-realm-v2",
                        plugin="realm_plugin_v2.py:RealmPlugin",
                    ),
                ),
                base=bundle_dir,
            )
        )
        assert presets.revisions("writer") == [1, 2]
        assert presets.revision_of("writer") == 2
        latest = presets.resolve("writer")
        assert latest.revision == 2
        assert latest.model == "writer-model-v2"
        assert presets.resolve_revision("writer", 1).model == "writer-model"
        assert [spec.revision for spec in presets.all() if spec.id == "writer"] == [2]
        with pytest.raises(PluginDependencyError):
            presets.resolve("writer", revision=99)
        with pytest.raises(DeeppyroError):
            presets.resolve("nope")
        assert presets.get(None) is None
    finally:
        await app.stop()


async def test_scope_and_revision_filtering(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "writer",
        PRESET_PLUGIN,
        {"realm_plugin.py": REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        presets = app.service("presets")
        await presets.ensure_all()
        revision = presets.resolve("writer").revision
        tools = app.service("tools")

        scoped = tools.registry_for(str(tmp_path), scope="writer", revision=revision)
        assert {"writer_note", "realm_note"} <= set(scoped.names())
        # Without a revision only revision-less scoped contributions are visible.
        legacy = tools.registry_for(str(tmp_path), scope="writer")
        assert "writer_note" in legacy
        assert "realm_note" not in legacy
        # Other scopes and the host composition never see the realm.
        assert "realm_note" not in tools.registry_for(str(tmp_path))
        assert "realm_note" not in tools.registry_for(str(tmp_path), scope="other", revision=revision)

        prompt_service = app.service("prompt")
        context = _prompt_context(tmp_path)
        pinned = prompt_service.build([], context, scope="writer", revision=revision)
        assert "## Realm hint" in pinned
        assert "## Writer mode" in pinned
        assert "## Realm hint" not in prompt_service.build([], context, scope="writer")
        assert "## Realm hint" not in prompt_service.build([], context)
    finally:
        await app.stop()


async def test_live_agent_keeps_its_revision(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "writer",
        PRESET_PLUGIN,
        {"realm_plugin.py": REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        presets = app.service("presets")
        await presets.ensure_all()
        first = _make_agent(app, tmp_path, preset="writer")
        assert first.preset == "writer"
        assert first.preset_revision == 1
        assert first.preset_spec.revision == 1
        assert first._options.model == "writer-model"
        assert first._options.instructions == ["Write clearly."]
        assert {"writer_note", "realm_note"} <= set(first._registry.names())
        assert "## Realm hint" in first._prompt_builder([], _prompt_context(tmp_path))

        (bundle_dir / "realm_plugin_v2.py").write_text(REALM_PLUGIN_V2, encoding="utf-8")
        presets.add(
            PresetSpec(
                id="writer",
                name="Writer",
                model="writer-model-v2",
                order=30,
                plugins=(PresetPluginRow(id="writer-realm-v2", plugin="realm_plugin_v2.py:RealmPlugin"),),
                base=bundle_dir,
            )
        )
        await presets.ensure("writer")
        second = _make_agent(app, tmp_path, preset="writer")
        assert second.preset_revision == 2
        assert second._options.model == "writer-model-v2"
        assert "realm_note_v2" in second._registry.names()
        assert "realm_note" not in second._registry.names()
        assert "## Realm v2" in second._prompt_builder([], _prompt_context(tmp_path))

        # The live agent still has the composition it was created with.
        assert "realm_note" in first._registry.names()
        assert "realm_note_v2" not in first._registry.names()
        assert "## Realm v2" not in first._prompt_builder([], _prompt_context(tmp_path))
        assert presets.is_materialized("writer", 1)
    finally:
        await app.stop()


async def test_failed_realm_row_is_isolated(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "broken",
        BROKEN_PLUGIN,
        {"realm_plugin.py": REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        presets = app.service("presets")
        await presets.ensure("broken")
        assert presets.is_materialized("broken")
        failures = presets.realm_failures("broken")
        assert [failure.row_id for failure in failures] == ["bad-row"]
        assert "MissingPlugin" in failures[0].error or "missing_plugin.py" in failures[0].error

        revision = presets.resolve("broken").revision
        tools = app.service("tools").registry_for(str(tmp_path), scope="broken", revision=revision)
        assert "realm_note" in tools.names()
    finally:
        await app.stop()


async def test_realm_tool_may_collide_with_host_tool(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "collide",
        COLLISION_PLUGIN,
        {"realm_plugin.py": COLLIDING_REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        presets = app.service("presets")
        await presets.ensure("collide")
        revision = presets.resolve("collide").revision
        registry = app.service("tools").registry_for(str(tmp_path), scope="collide", revision=revision)
        assert registry.names().count("shared_host") == 1
        result = await registry.get("shared_host").run({}, None)
        assert result.text == "host"
    finally:
        await app.stop()


async def test_realm_contributions_are_removed_with_the_revision(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "writer",
        PRESET_PLUGIN,
        {"realm_plugin.py": REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        presets = app.service("presets")
        await presets.ensure("writer")
        first = presets.resolve("writer")
        remove = presets.add(
            PresetSpec(
                id="writer",
                name="Writer",
                model="writer-model-v2",
                order=30,
                plugins=(PresetPluginRow(id="writer-realm-v2", plugin="realm_plugin_v2.py:RealmPlugin"),),
                base=bundle_dir,
            )
        )
        (bundle_dir / "realm_plugin_v2.py").write_text(REALM_PLUGIN_V2, encoding="utf-8")
        await presets.ensure("writer")
        assert presets.revision_of("writer") == 2
        remove()
        assert presets.revision_of("writer") == 1
        assert presets.revisions("writer") == [1]
        assert presets.is_materialized("writer", 1)
        tools = app.service("tools")
        assert (
            "realm_note" in tools.registry_for(str(tmp_path), scope="writer", revision=first.revision).names()
        )
        # The sync remover schedules realm teardown; let it run.
        for _ in range(10):
            await asyncio.sleep(0)
        assert "realm_note_v2" not in tools.registry_for(str(tmp_path), scope="writer", revision=2).names()
    finally:
        await app.stop()


async def test_builtin_presets_offer_default_and_explicit_team_mode(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "writer",
        PRESET_PLUGIN,
        {"realm_plugin.py": REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        presets = app.service("presets")
        default = presets.resolve()
        assert default.id == "default"
        assert default.mode == AGENT_MODE_DEFAULT
        assert default.revision == 0

        team = presets.resolve(TEAM_PRESET_ID)
        assert team.id == TEAM_PRESET_ID
        assert team.name == "Team"
        assert team.mode == AGENT_MODE_TEAM
        assert team.order == 200
        assert team.revision == 0
        assert team.plugins == ()
        assert {"default", TEAM_PRESET_ID} <= set(presets.ids())
        assert presets.revision_of(TEAM_PRESET_ID) == 0
        assert presets.revisions(TEAM_PRESET_ID) == [0]
        # /preset team is simply resolving the built-in team preset.
        assert presets.resolve("team").mode == AGENT_MODE_TEAM
        assert [spec.id for spec in presets.all()][-1] == TEAM_PRESET_ID
    finally:
        await app.stop()


async def test_agent_mode_filters_delegation_tools(tmp_path: Path) -> None:
    bundle_dir = _write_bundle(
        tmp_path,
        "writer",
        PRESET_PLUGIN,
        {"realm_plugin.py": REALM_PLUGIN},
    )
    app = await _boot(tmp_path, bundle_dir)
    try:
        sessions = app.service("sessions")
        agents = app.service("agents")
        all_names = [*TEAM_TOOL_NAMES, *SUBAGENT_TOOL_NAMES, "read"]
        registry = ToolRegistry([_named_tool(name) for name in all_names])

        def make_agent(preset: str | None) -> Any:
            session = sessions.create(str(tmp_path), agent_preset=preset)
            return agents.create(
                session=session,
                provider=object(),
                registry=registry,
                options=agents.options(),
                sink=DummySink(),
                cwd=str(tmp_path),
                preset=preset,
            )

        team_agent = make_agent(TEAM_PRESET_ID)
        assert set(team_agent._registry.names()) == {*TEAM_TOOL_NAMES, "read"}
        assert "subagent" not in team_agent._registry

        default_agent = make_agent(None)
        assert set(default_agent._registry.names()) == {"subagent", "read"}
        assert not set(TEAM_TOOL_NAMES) & set(default_agent._registry.names())

        # Filtering never mutates the caller's registry.
        assert registry.names() == all_names
    finally:
        await app.stop()
