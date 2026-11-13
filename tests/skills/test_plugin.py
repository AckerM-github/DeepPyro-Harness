"""Bundled SkillsPlugin wiring: service, tool registration and prompt section."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.builtin.skills import SKILLS_PROMPT_ORDER
from deeppyro.config import Config
from deeppyro.kernel.app import create_app
from deeppyro.kernel.manifest import PluginSpec
from deeppyro.prompt.prompt import PromptContext
from deeppyro.skills.registry import SkillRegistry

from .helpers import make_project, write_skill

_SKILLS_ROW = PluginSpec(
    id="skills",
    name="deeppyro.builtin.skills:SkillsPlugin",
)
_FAKE_ROWS = (
    PluginSpec(id="fake-tools", name="tests.skills.plugins_dummy:FakeToolsPlugin"),
    PluginSpec(id="fake-prompt", name="tests.skills.plugins_dummy:FakePromptPlugin"),
)


def _config(**extra: Any) -> Config:
    data: dict[str, Any] = {
        "agent": {"provider": "local", "model": "m"},
        "providers": {
            "local": {
                "api": "openai",
                "baseURL": "http://127.0.0.1:8000/v1",
                "models": [{"id": "m"}],
            }
        },
    }
    data.update(extra)
    return Config.model_validate(data)


def _prompt_context(cwd: Path) -> PromptContext:
    return PromptContext(
        cwd=cwd,
        platform="test",
        session_id="session-test",
        started_at=0,
        permission_preset="ask",
        approval_policy="default",
        model="m",
    )


async def _boot(row: PluginSpec, config: Config | None = None) -> Any:
    return await create_app(config or _config(), plugins=(*_FAKE_ROWS, row))


async def test_plugin_wires_service_tool_and_prompt_section(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_skill(project / ".dsh" / "skills", "demo", description="Demo skill")
    app = await _boot(_SKILLS_ROW)
    try:
        service = app.service("skills")
        assert isinstance(service.registry, SkillRegistry)
        assert [item.name for item in service.list(project)] == ["demo"]
        assert service.snapshot(project).errors == ()

        tools = app.service("tools")
        assert len(tools.tool_factories) == 1
        factory = tools.tool_factories[0][0]
        built = factory(None, str(project))
        assert [tool.name for tool in built] == ["skill"]

        prompt = app.service("prompt")
        assert len(prompt.sections) == 1
        name, order, renderer, plugin_id = prompt.sections[0]
        assert name == "skills"
        assert order == SKILLS_PROMPT_ORDER == 35
        assert plugin_id == "skills"
        rendered = renderer([], _prompt_context(project))
        assert "<available_skills>" in rendered
        assert "- demo: Demo skill" in rendered
        assert app.started["skills"] == "ACTIVE"
    finally:
        await app.stop()


async def test_plugin_row_config_overrides_global_table(tmp_path: Path) -> None:
    external = tmp_path / "external-skills"
    write_skill(external, "outside", description="Outside skill")
    project = make_project(tmp_path)
    row = PluginSpec(
        id="skills",
        name="deeppyro.builtin.skills:SkillsPlugin",
        config={"custom_skill_dirs": [str(external)]},
    )
    app = await _boot(row)
    try:
        service = app.service("skills")
        assert service.skills_config.custom_skill_dirs == [str(external)]
        assert [item.name for item in service.list(project)] == ["outside"]
    finally:
        await app.stop()


async def test_prompt_section_swallows_render_errors(tmp_path: Path, monkeypatch: Any) -> None:
    project = make_project(tmp_path)
    app = await _boot(_SKILLS_ROW)
    try:
        service = app.service("skills")

        class BoomRegistry:
            def render_catalog(self, *args: Any, **kwargs: Any) -> str:
                raise RuntimeError("boom")

        monkeypatch.setattr(service, "_registry", BoomRegistry())
        assert service._render_section([], _prompt_context(project)) == ""
    finally:
        await app.stop()
