"""Integration: plugin services reach the agent tools and the TUI slot host.

Layer D exposes plugin_manager / inspect / settings as cordis services and four
agent-facing tools. Layer E exposes the `slots` registry. This module proves
both are wired end to end without network access.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.builtin.activity import ActivityService
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.jobs import JobsService
from deeppyro.kernel import boot
from deeppyro.session import SessionStore
from deeppyro.team.manager import TeamService
from tests.integration.fake_provider import Response, ScriptedProvider, tool_response

SLOTS_SOURCE = 'from deeppyro.api import Plugin\n\n\nclass DemoslotsPlugin(Plugin):\n    plugin_id = "demo-slots"\n    version = "0.1.0"\n    inject = ["slots"]\n\n    def setup(self, ctx, config):\n        registry = ctx.service("slots")\n        registry.add_status_item(lambda context: "demo: on", id="demo-status", plugin_id="demo-slots")\n        registry.add_panel("Demo panel", lambda context: None, id="demo-panel", plugin_id="demo-slots")\n'
SLOTS_BUNDLE = '[bundle]\nid = "demo-slots-bundle"\nname = "Demo slots bundle"\nversion = "0.1.0"\norder = 60\n\n[[plugins]]\nid = "demo-slots"\nplugin = "demo_slots.py:DemoslotsPlugin"\n'


def _config() -> Config:
    return Config(
        providers={"local": ProviderConfig(baseURL="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])},
        agent={"provider": "local", "model": "m"},
    )


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)


async def test_plugin_tools_run_with_kernel_services(tmp_path: Path) -> None:
    app = await boot(_config())
    try:
        registry = app.service("tools").registry_for(tmp_path)
        for name in ("plugin_manager", "plugin_inspect", "settings_read", "settings_write"):
            assert name in registry, name

        services = {name: app.service(name) for name in ("plugin_manager", "inspect", "settings")}
        store = SessionStore(tmp_path / "sessions")
        session = store.create(tmp_path)
        provider = ScriptedProvider(
            [
                tool_response("call-1", "plugin_inspect", '{"what":"services"}'),
                Response(text="done"),
            ]
        )
        agent = AgentSession(
            session=session,
            provider=provider,
            registry=registry,
            options=AgentOptions(max_steps=4, model="scripted-model"),
            sink=_Sink(),
            cwd=tmp_path,
            services=services,
        )
        reply = await agent.send("list the services")
        assert reply == "done"

        results = [record for record in session.records if record.type == "tool/result"]
        assert len(results) == 1
        message = results[0].data["message"]
        assert message["isError"] is False
        text = message["content"][0]["text"]
        assert "plugin_manager" in text
        assert "slots" in text
    finally:
        await app.stop()


async def test_runtime_services_expose_manager_objects(tmp_path: Path) -> None:
    """The service registry must expose managers, not the plugin wrappers.

    Regression: JobsPlugin used to be a cordis Service, so service("jobs")
    returned the plugin object (which has no start/list) and every job tool
    failed with "'JobsPlugin' object has no attribute 'start'".
    """

    app = await boot(_config())
    try:
        jobs = app.service("jobs")
        team = app.service("team")
        activity = app.service("activity")
        assert isinstance(jobs, JobsService)
        assert callable(jobs.start) and callable(jobs.list)
        assert isinstance(team, TeamService)
        assert callable(team.spawn) and callable(team.tasks)
        assert isinstance(activity, ActivityService)
        assert callable(activity.refresh)
        registry = app.service("tools").registry_for(tmp_path)
        assert "job_list" in registry
        assert "team_task_list" in registry
    finally:
        await app.stop()


async def test_external_bundle_registers_tui_slots(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "demo-slots"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(SLOTS_BUNDLE, encoding="utf-8")
    (bundle_dir / "demo_slots.py").write_text(SLOTS_SOURCE, encoding="utf-8")
    app = await boot(_config(), bundle_paths=[str(bundle_dir)])
    try:
        registry = app.service("slots")
        assert [item.id for item in registry.status_items()] == ["demo-status"]
        assert [item.id for item in registry.panels()] == ["demo-panel"]
        assert app.started["demo-slots"] == "ACTIVE"
    finally:
        await app.stop()
