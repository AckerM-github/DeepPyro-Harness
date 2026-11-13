"""Bundled MemoryPlugin wiring: service, tool registration and prompt section.

The plugin is booted through the real kernel with fake tools/prompt services so
the test observes exactly the registrations the memory plugin makes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.builtin.memory import (
    MEMORY_PROMPT_ORDER,
    MEMORY_PROMPT_SECTION,
    SNAPSHOT_HEADER,
    SNAPSHOT_NOTE,
    MemoryPlugin,
    render_memory_snapshot,
)
from deeppyro.config import Config
from deeppyro.kernel.app import create_app
from deeppyro.kernel.manifest import PluginSpec
from deeppyro.memory.service import MemoryService
from deeppyro.memory.store import MemoryStore
from deeppyro.prompt.prompt import PromptContext

_MEMORY_ROW = PluginSpec(id="memory", name="deeppyro.builtin.memory:MemoryPlugin")
_FAKE_ROWS = (
    PluginSpec(id="fake-tools", name="tests.memory.plugins_dummy:FakeToolsPlugin"),
    PluginSpec(id="fake-prompt", name="tests.memory.plugins_dummy:FakePromptPlugin"),
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


def _prompt_context() -> PromptContext:
    return PromptContext(
        cwd=Path("."),
        platform="test",
        session_id="session-test",
        started_at=0,
        permission_preset="ask",
        approval_policy="default",
        model="m",
    )


async def _boot() -> Any:
    return await create_app(_config(), plugins=(*_FAKE_ROWS, _MEMORY_ROW))


async def test_plugin_wires_service_tool_and_prompt_section(isolated_home: Path) -> None:
    app = await _boot()
    try:
        service = app.service("memory")
        assert isinstance(service, MemoryService)
        assert service.store.root == isolated_home / "memory"

        tools = app.service("tools")
        assert len(tools.tool_factories) == 1
        factory = tools.tool_factories[0][0]
        assert factory(None, ".")[0].name == "mnemon_runtime_memory"

        prompt = app.service("prompt")
        assert len(prompt.sections) == 1
        name, order, renderer, plugin_id = prompt.sections[0]
        assert name == MEMORY_PROMPT_SECTION == "memory-runtime"
        assert order == MEMORY_PROMPT_ORDER == 12
        assert plugin_id == "memory"
        assert renderer([], _prompt_context()) == ""
        assert app.started["memory"] == "ACTIVE"
    finally:
        await app.stop()


async def test_prompt_section_renders_snapshot() -> None:
    app = await _boot()
    try:
        service = app.service("memory")
        prompt = app.service("prompt")
        renderer = prompt.sections[0][2]

        service.remember("user", "prefers concise replies")
        rendered = renderer([], _prompt_context())

        assert rendered.startswith(SNAPSHOT_HEADER)
        assert SNAPSHOT_NOTE in rendered
        assert '<runtime-memory-file name="USER.md">' in rendered
        assert "prefers concise replies" in rendered
        assert "MEMORY.md" not in rendered

        service.remember("memory", "tests run with uv")
        rendered = renderer([], _prompt_context())
        assert '<runtime-memory-file name="MEMORY.md">' in rendered
        assert "tests run with uv" in rendered
        assert rendered.index("USER.md") < rendered.index("MEMORY.md")
        assert rendered.count("<runtime-memory-file") == 2
        assert rendered == render_memory_snapshot(service)
    finally:
        await app.stop()


async def test_prompt_section_swallows_render_errors(monkeypatch: Any) -> None:
    app = await _boot()
    try:
        service = app.service("memory")
        renderer = app.service("prompt").sections[0][2]

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(service, "snapshot", boom)

        assert renderer([], _prompt_context()) == ""
        assert app.service("memory") is service
    finally:
        await app.stop()


async def test_memory_survives_kernel_restart() -> None:
    first = await _boot()
    try:
        first.service("memory").remember("user", "likes tea")
    finally:
        await first.stop()

    second = await _boot()
    try:
        service = second.service("memory")
        assert service.snapshot("user").text == "likes tea"
        assert service.revision == 1
    finally:
        await second.stop()


def test_render_memory_snapshot_unit(tmp_path: Path) -> None:
    service = MemoryService(store=MemoryStore(tmp_path))
    assert render_memory_snapshot(service) == ""
    service.remember("user", "likes tea")
    assert render_memory_snapshot(service).startswith(SNAPSHOT_HEADER)
    assert MemoryPlugin.plugin_id == "memory"
    assert MemoryPlugin.provide == "memory"
    assert MemoryPlugin.inject == ["prompt"]
