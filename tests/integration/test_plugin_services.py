"""Integration: plugin services reach the agent tools and the TUI slot host.

Layer D exposes plugin_manager / inspect / settings as kernel services and four
agent-facing tools. Layer E exposes the `slots` registry. This module proves
both are wired end to end without network access.
"""

from __future__ import annotations

from pathlib import Path

from deeppyro.builtin.activity import ActivityService
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.jobs import JobsService
from deeppyro.kernel import boot
from deeppyro.team.manager import TeamService
from deeppyro.tui.slots import SlotRegistry

SLOTS_SOURCE = 'from deeppyro.api import Plugin\nfrom deeppyro.tui.slots import SlotRegistry\n\n\nclass DemoslotsPlugin(Plugin):\n    plugin_id = "demo-slots"\n    version = "0.1.0"\n    inject = ["slots"]\n\n    def setup(self):\n        registry = self.ctx.require(SlotRegistry)\n        registry.add_status_item(lambda context: "demo: on", id="demo-status", plugin_id="demo-slots")\n        registry.add_panel("Demo panel", lambda context: None, id="demo-panel", plugin_id="demo-slots")\n'
SLOTS_BUNDLE = '[bundle]\nid = "demo-slots-bundle"\nname = "Demo slots bundle"\nversion = "0.1.0"\norder = 60\n\n[[plugins]]\nid = "demo-slots"\nplugin = "demo_slots.py:DemoslotsPlugin"\n'


def _config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(base_url="http://127.0.0.1:8000/v1", models=[ModelConfig(id="m")])
        },
        agent={"provider": "local", "model": "m"},
    )


async def test_runtime_services_expose_manager_objects(tmp_path: Path) -> None:
    """The service registry must expose managers, not the plugin wrappers.

    Regression: JobsPlugin used to inherit the Service base, so the jobs
    contract returned the plugin object (which has no start/list) and every job
    tool failed with "'JobsPlugin' object has no attribute 'start'".
    """

    app = await boot(_config())
    try:
        jobs = app.require(JobsService)
        team = app.require(TeamService)
        activity = app.require(ActivityService)
        assert isinstance(jobs, JobsService)
        assert callable(jobs.start) and callable(jobs.list)
        assert isinstance(team, TeamService)
        assert callable(team.spawn) and callable(team.tasks)
        assert isinstance(activity, ActivityService)
        assert callable(activity.refresh)
        registry = app.require(ToolsPlugin).registry_for(tmp_path)
        assert "job_list" in registry
        # Team tools are scoped to the explicit team preset, so they are absent
        # from the host-wide registry but present in the team composition.
        assert "team_task_list" not in registry
        team_registry = app.require(ToolsPlugin).registry_for(tmp_path, scope="team")
        assert "team_task_list" in team_registry
    finally:
        await app.stop()


async def test_external_bundle_registers_tui_slots(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "demo-slots"
    bundle_dir.mkdir()
    (bundle_dir / "bundle.toml").write_text(SLOTS_BUNDLE, encoding="utf-8")
    (bundle_dir / "demo_slots.py").write_text(SLOTS_SOURCE, encoding="utf-8")
    app = await boot(_config(), bundle_paths=[str(bundle_dir)])
    try:
        registry = app.require(SlotRegistry)
        assert [item.id for item in registry.status_items()] == ["demo-status"]
        assert [item.id for item in registry.panels()] == ["demo-panel"]
        assert app.started["demo-slots"] == "ACTIVE"
    finally:
        await app.stop()
