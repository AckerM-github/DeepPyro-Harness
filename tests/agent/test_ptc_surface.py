"""PTC presentation surface: what the model sees directly vs. through run_code.

Two layers are covered here:

* AgentSession honors an explicit surface_registry: the request tools, the
  dispatch registry and the prompt's direct-tool list narrow to it, while the
  system prompt and the tool_registry service keep the full registry (the
  nested dispatcher needs every tool).
* AgentsPlugin decides the surface from the live PTC service and the preset
  mode, so a default-mode agent in PTC mode sees only run_code and a team-mode
  preset keeps the native surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.api import TEAM_PRESET_ID
from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.protocols import TextDelta, ToolCallArgsDelta, ToolCallStart, ToolResult
from deeppyro.kernel import boot
from deeppyro.ptc import RunCodeTool
from deeppyro.tools.registry import ToolRegistry

from .fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)


class _ImageTool(FakeTool):
    """Tool that reports one image part next to its text."""

    def __init__(self, name: str = "screenshot") -> None:
        super().__init__(name, text="captured")
        self.attachment = "attachment-1"

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        self.calls.append((dict(args), ctx))
        return ToolResult(
            text="captured",
            parts=[{"type": "image", "attachment": self.attachment}],
        )


def _make_agent(
    tmp_path: Path,
    registry: FakeRegistry,
    script: list[ScriptedTurn],
    *,
    surface: Any | None = None,
    prompt_builder: Any | None = None,
) -> tuple[FakeSessionLog, ScriptedProvider, AgentSession]:
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=registry,
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        prompt_builder=prompt_builder,
        surface_registry=surface,
    )
    return session, provider, agent


def _names(schemas: list[Any]) -> list[str]:
    return [schema.name for schema in schemas]


def _capture_prompt(seen: list[tuple[list[str], Any]]) -> Any:
    def builder(tools: list[Any], ctx: Any) -> str:
        seen.append((_names(tools), ctx))
        return "SYSTEM"

    return builder


async def test_native_surface_offers_every_tool(tmp_path: Path) -> None:
    echo = FakeTool("echo")
    read = FakeTool("read")
    registry = FakeRegistry([echo, read])
    seen: list[tuple[list[str], Any]] = []
    session, provider, agent = _make_agent(
        tmp_path,
        registry,
        [ScriptedTurn(events=[TextDelta("hi"), finish("stop")])],
        prompt_builder=_capture_prompt(seen),
    )

    assert await agent.send("go") == "hi"

    assert _names(provider.requests[0].tools) == ["echo", "read"]
    assert seen[0][0] == ["echo", "read"]
    assert seen[0][1].direct_tool_names == ("echo", "read")
    assert agent.surface_registry is None
    assert agent._services["tool_registry"] is registry
    header = session.records_of("request/header")[0].data["header"]
    assert [tool["name"] for tool in header["tools"]] == ["echo", "read"]


async def test_ptc_surface_narrows_request_and_prompt_direct_list(tmp_path: Path) -> None:
    full = FakeRegistry([FakeTool("echo"), FakeTool("read"), FakeTool("run_code")])
    surface = full.without(["echo", "read"])
    seen: list[tuple[list[str], Any]] = []
    session, provider, agent = _make_agent(
        tmp_path,
        full,
        [ScriptedTurn(events=[TextDelta("done"), finish("stop")])],
        surface=surface,
        prompt_builder=_capture_prompt(seen),
    )

    assert await agent.send("go") == "done"

    # The request and its durable header only advertise the presentation tool.
    assert _names(provider.requests[0].tools) == ["run_code"]
    header = session.records_of("request/header")[0].data["header"]
    assert [tool["name"] for tool in header["tools"]] == ["run_code"]

    # The system prompt is still rendered from the full registry so the
    # tools:sdk section can document every nested tool.
    assert seen[0][0] == ["echo", "read", "run_code"]

    # Only the direct list narrows: nested dispatch keeps the full registry.
    assert seen[0][1].direct_tool_names == ("run_code",)
    assert agent._services["tool_registry"] is full
    assert agent._registry is full
    assert agent._surface is surface


async def test_ptc_dispatch_uses_the_surface_tool(tmp_path: Path) -> None:
    full_run_code = FakeTool("run_code", text="full")
    full = FakeRegistry([FakeTool("echo"), full_run_code])
    surface_run_code = FakeTool("run_code", text="surface")
    surface = FakeRegistry([surface_run_code])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "run_code"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _provider, agent = _make_agent(tmp_path, full, script, surface=surface)

    assert await agent.send("go") == "done"

    assert len(surface_run_code.calls) == 1
    assert full_run_code.calls == []
    results = session.records_of("tool/result")
    assert len(results) == 1
    assert results[0].data["message"]["content"][0]["text"] == "surface"


async def test_image_tool_parts_become_a_user_message(tmp_path: Path) -> None:
    image_tool = _ImageTool()
    registry = FakeRegistry([image_tool])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "screenshot"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _provider, agent = _make_agent(tmp_path, registry, script)

    assert await agent.send("look") == "done"

    # The tool message stays text-only: the wire contract of tool/result is
    # unchanged, including when the tool produced non-text parts.
    tool_result = session.records_of("tool/result")[0]
    content = tool_result.data["message"]["content"]
    assert [part["type"] for part in content] == ["text"]
    assert content[0]["text"] == "captured"

    # The image rides in a follow-up user message right after the result.
    records = session.records
    index = next(i for i, record in enumerate(records) if record.type == "tool/result")
    assert records[index + 1].type == "user/message"
    followup = records[index + 1].data
    assert followup["role"] == "user"
    assert followup["source"] == {"kind": "ptc-mode"}
    assert followup["content"] == [{"type": "image", "attachment": "attachment-1"}]

    # ... and it participates in the ordinary surface fold, immediately after
    # the tool message (later steps may append their own runtime context).
    derived = session.derive_messages()
    tool_index = max(index for index, message in enumerate(derived) if message.role == "tool")
    followup_message = derived[tool_index + 1]
    assert followup_message.role == "user"
    assert followup_message.source == {"kind": "ptc-mode"}
    assert followup_message.content[0].type == "image"


async def test_text_only_tool_results_do_not_add_a_user_message(tmp_path: Path) -> None:
    echo = FakeTool("echo", text="plain")
    registry = FakeRegistry([echo])
    script = [
        ScriptedTurn(
            events=[
                ToolCallStart("call_1", "echo"),
                ToolCallArgsDelta("call_1", "{}"),
                finish("tool-calls"),
            ]
        ),
        ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
    ]
    session, _provider, agent = _make_agent(tmp_path, registry, script)

    assert await agent.send("go") == "done"

    user_messages = session.records_of("user/message")
    assert not [record for record in user_messages if record.data.get("source", {}).get("kind") == "ptc-mode"]


# -- AgentsPlugin surface selection ----------------------------------------


class _DummySink:
    def emit(self, event: dict[str, Any]) -> None:
        pass


class _FakePtc:
    def __init__(self, mode: str = "ptc") -> None:
        self.mode = mode


class _PluginContext:
    """Plugin context over a booted app with a fake ptc service."""

    def __init__(self, app: Any, ptc: Any) -> None:
        self._app = app
        self._ptc = ptc

    def service(self, name: str) -> Any:
        return self._app.service(name)

    def get(self, name: str) -> Any | None:
        if name == "ptc":
            return self._ptc
        try:
            return self._app.service(name)
        except Exception:  # noqa: BLE001 - optional service lookup
            return None


class _FakeTools:
    """Tools service stand-in whose registry_for returns one fixed registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    def registry_for(self, cwd: Any, *, scope: Any = None, revision: Any = None) -> Any:
        return self._registry


def _plugin(app: Any, ptc: Any) -> AgentsPlugin:
    """Build the agents service over a booted app with a chosen ptc service.

    The cordis Service base registers itself on ctx.reflect, so the real plugin
    is created by the kernel; this helper binds the same collaborators on a bare
    instance to exercise the surface decision with a controlled ptc service.
    """
    plugin = object.__new__(AgentsPlugin)
    plugin._ctx = _PluginContext(app, ptc)
    plugin._presets = app.service("presets")
    plugin._prompt_service = app.service("prompt")
    plugin._tools = app.service("tools")
    plugin._agent_config = app.service("config").value.agent
    return plugin


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={"local": ProviderConfig(baseURL="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])},
        agent={"provider": "local", "model": "m"},
        sessionDir=tmp_path / "sessions",
    )


def _create(agent_service: AgentsPlugin, app: Any, tmp_path: Path, **kwargs: Any) -> Any:
    session = app.service("sessions").create(str(tmp_path))
    return agent_service.create(
        session=session,
        provider=object(),
        options=agent_service.options(),
        sink=_DummySink(),
        cwd=str(tmp_path),
        **kwargs,
    )


async def test_agents_plugin_surface_selection(tmp_path: Path) -> None:
    app = await boot(_config(tmp_path))
    try:
        base = app.service("tools").registry_for(tmp_path)
        other = base.names()[0]
        registry = ToolRegistry([base.get(other), RunCodeTool()])

        # PTC mode + default preset + run_code registered: narrow to run_code.
        ptc_plugin = _plugin(app, _FakePtc("ptc"))
        agent = _create(ptc_plugin, app, tmp_path, registry=registry)
        assert agent._registry.names() == [other, "run_code"]
        assert agent.surface_registry is not None
        assert agent.surface_registry.names() == ["run_code"]
        assert agent._surface is agent.surface_registry
        assert agent.surface_registry.get("run_code") is not None

        # Team presets keep the native surface even in PTC mode.
        team = _create(ptc_plugin, app, tmp_path, registry=registry, preset=TEAM_PRESET_ID)
        assert team.surface_registry is None
        assert team._surface is team._registry

        # Native mode (real ptc service default) keeps every tool.
        native = _create(app.service("agents"), app, tmp_path, registry=registry)
        assert native.surface_registry is None
        assert native._surface is native._registry

        # A missing ptc service also defaults to native behaviour.
        missing = _plugin(app, None)
        assert _create(missing, app, tmp_path, registry=registry).surface_registry is None

        # Default registry path (registry= omitted): the same surface decision
        # applies to the registry the tools service hands out.
        default_plugin = _plugin(app, _FakePtc("ptc"))
        default_plugin._tools = _FakeTools(registry)
        default_agent = _create(default_plugin, app, tmp_path)
        assert default_agent.surface_registry is not None
        assert default_agent.surface_registry.names() == ["run_code"]

        # ... but the kernel registry has no run_code here, so PTC mode cannot
        # narrow (run_code must be registered to be presented).
        unregistered = _create(_plugin(app, _FakePtc("ptc")), app, tmp_path)
        assert unregistered.surface_registry is None
    finally:
        await app.stop()
