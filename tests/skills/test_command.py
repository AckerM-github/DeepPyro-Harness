"""The /skill command: list user-invocable skills and load one into the turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.builtin.commands import CommandsPlugin
from deeppyro.builtin.skills import SkillsPlugin
from deeppyro.config import Config
from deeppyro.kernel.app import create_app
from deeppyro.kernel.manifest import PluginSpec

from .helpers import make_project, write_skill

_ROW = PluginSpec(id="skills", name="deeppyro.builtin.skills:SkillsPlugin")
_FAKE_ROWS = (
    PluginSpec(id="commands", name="deeppyro.builtin.commands:CommandsPlugin"),
    PluginSpec(id="fake-tools", name="tests.skills.plugins_dummy:FakeToolsPlugin"),
    PluginSpec(id="fake-prompt", name="tests.skills.plugins_dummy:FakePromptPlugin"),
)


def _config() -> Config:
    return Config.model_validate(
        {
            "agent": {"provider": "local", "model": "m"},
            "providers": {
                "local": {
                    "api": "openai",
                    "baseURL": "http://127.0.0.1:8000/v1",
                    "models": [{"id": "m"}],
                }
            },
        }
    )


class _Agent:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> str:
        self.sent.append(text)
        return "ok"


class _Runtime:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd
        self.agent = _Agent()


class _Conversation:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.errors: list[str] = []

    def add_info(self, text: str) -> None:
        self.info.append(text)

    def add_error(self, text: str) -> None:
        self.errors.append(text)


class _Host:
    def __init__(self, cwd: Path, *, agent: bool = True) -> None:
        self.conversation = _Conversation()
        self.runtime = _Runtime(cwd)
        if not agent:
            self.runtime.agent = None  # type: ignore[assignment]


async def _boot() -> Any:
    return await create_app(_config(), plugins=(*_FAKE_ROWS, _ROW))


def _spec(app: Any, name: str) -> Any:
    return next(spec for spec in app.require(CommandsPlugin).all() if spec.name == name)


async def test_command_is_registered_with_usage(tmp_path: Path) -> None:
    app = await _boot()
    try:
        spec = _spec(app, "skill")
        assert spec.summary.startswith("list or load a skill")
        assert spec.usage == "/skill [name] [arguments]"
        assert spec.plugin_id == "skills"
    finally:
        await app.stop()


async def test_listing_shows_only_user_invocable_skills(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    root = project / ".dsh" / "skills"
    write_skill(root, "open-one", description="Anyone can run me")
    write_skill(
        root,
        "model-only",
        description="Model only",
        extra="user-invocable: false",
    )
    app = await _boot()
    try:
        host = _Host(project)
        await _spec(app, "skill").handler(host, "")
        listing = host.conversation.info[-1]
        assert "open-one — Anyone can run me" in listing
        assert "model-only" not in listing
    finally:
        await app.stop()


async def test_loading_sends_the_body_and_arguments(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_skill(project / ".dsh" / "skills", "demo", body="Step one. Step two.")
    app = await _boot()
    try:
        host = _Host(project)
        await _spec(app, "skill").handler(host, "demo tighten the loop")
        sent = host.runtime.agent.sent[-1]
        assert sent.startswith("# Skill: demo")
        assert "Step one. Step two." in sent
        assert sent.endswith("User arguments: tighten the loop")
        assert host.conversation.info == []
    finally:
        await app.stop()


async def test_unknown_skill_is_reported(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_skill(project / ".dsh" / "skills", "demo")
    app = await _boot()
    try:
        host = _Host(project)
        await _spec(app, "skill").handler(host, "nope")
        assert host.conversation.info[-1] == "unknown skill: nope (try /skill)"
        assert host.runtime.agent.sent == []
    finally:
        await app.stop()


async def test_model_only_skill_cannot_be_invoked(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_skill(project / ".dsh" / "skills", "quiet", extra="user-invocable: false")
    app = await _boot()
    try:
        host = _Host(project)
        await _spec(app, "skill").handler(host, "quiet")
        assert host.conversation.info[-1] == "skill 'quiet' is not user-invocable"
        assert host.runtime.agent.sent == []
    finally:
        await app.stop()


async def test_a_host_without_an_agent_still_shows_the_body(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    write_skill(project / ".dsh" / "skills", "demo", body="Body text.")
    app = await _boot()
    try:
        host = _Host(project, agent=False)
        await _spec(app, "skill").handler(host, "demo")
        assert host.conversation.info[-1].startswith("# Skill: demo")
    finally:
        await app.stop()


def test_command_lives_on_the_plugin() -> None:
    assert hasattr(SkillsPlugin, "_command")
