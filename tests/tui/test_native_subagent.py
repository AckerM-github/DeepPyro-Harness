"""Native subagent runs in the TUI: facts projection, panel rows, nested guard.

Native subagents are recorded as `plugin.subagent.*` plugin events on the parent
session; the activity bar and team panel project them from those facts, with the
measured usage from the subagent service or an explicit "usage not recorded".
A subagent that tries to spawn another subagent fails with a clear message
instead of crashing the tool call.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.subagent import SubagentError
from deeppyro.subagent.native_manager import _CHILD_RUN, NESTED_ERROR, NativeSubagentService
from deeppyro.tui.app_panels import PanelLayer
from deeppyro.tui.native_activity import (
    COMPLETED,
    FAILED,
    RUNNING,
    activity_records,
    refresh_activity,
    subagent_runs,
    team_activity_records,
)
from deeppyro.tui.screens.team import TeamPanelScreen, normalize_subagent, subagent_lines
from tests.compact.test_native_compactor import SessionBuilder

STARTED = "plugin.subagent.started"
FINISHED = "plugin.subagent.finished"
MEMBER = "plugin.team.member.changed"


def _session(tmp_path: Path) -> tuple[NativeSessionHandle, SessionBuilder]:
    builder = SessionBuilder(tmp_path / "session-00000000-0000-0000-0000-000000000001")
    return NativeSessionHandle(builder.session), builder


def _started(handle: NativeSessionHandle, child_id: str = "child-1", **extra: Any) -> None:
    data: dict[str, Any] = {
        "version": 1,
        "child_session_id": child_id,
        "label": "scout",
        "provider": "local",
        "model": "scripted-1",
        "mode": "spawn",
        "created_at": 1700000000,
    }
    data.update(extra)
    handle.native_session.append(STARTED, data)


def _finished(handle: NativeSessionHandle, child_id: str = "child-1", **extra: Any) -> None:
    data: dict[str, Any] = {
        "version": 1,
        "child_session_id": child_id,
        "status": "completed",
        "is_error": False,
        "usage_known": True,
    }
    data.update(extra)
    handle.native_session.append(FINISHED, data)


async def test_nested_subagent_start_is_refused_with_a_clear_message() -> None:
    service = NativeSubagentService()
    token = _CHILD_RUN.set(1)
    try:
        with pytest.raises(SubagentError, match="nested subagent delegation"):
            await service.start("do more work")
    finally:
        _CHILD_RUN.reset(token)
    # The guard is scoped: after the child run settles a new run is allowed
    # through the guard (it then fails for the honest missing-parent reason).
    assert NESTED_ERROR == (
        "nested subagent delegation is not supported on the native runtime; "
        "a subagent cannot start another subagent"
    )


async def test_native_activity_folds_subagent_facts_and_usage(
    tmp_path: Path,
) -> None:
    handle, _builder = _session(tmp_path)
    _started(handle, "child-1")
    _finished(handle, "child-1")
    _started(handle, "child-2", label="critic", mode="fork")
    service = SimpleNamespace(
        usage=lambda child_id: (
            SimpleNamespace(input_tokens=12, output_tokens=7, cached_tokens=3)
            if child_id == "child-1"
            else None
        )
    )

    runs = {run.child_id: run for run in subagent_runs(handle, service)}
    assert runs["child-1"].status == COMPLETED
    assert runs["child-1"].label == "scout"
    assert runs["child-1"].usage_text() == "usage 12 in / 7 out / 3 cached"
    assert runs["child-2"].status == RUNNING
    assert runs["child-2"].label == "critic"
    assert runs["child-2"].mode == "fork"
    assert runs["child-2"].usage_text() == "usage not recorded"

    # Without a live service (cold restore) usage is explicitly not recorded.
    cold = {run.child_id: run for run in subagent_runs(handle)}
    assert cold["child-1"].status == COMPLETED
    assert cold["child-1"].usage_text() == "usage not recorded"


def test_native_activity_records_feed_the_activity_service(tmp_path: Path) -> None:
    handle, _builder = _session(tmp_path)
    handle.native_session.append(
        MEMBER,
        {
            "version": 1,
            "team_id": "team",
            "member_id": "member-1",
            "name": "scout",
            "description": "reads code",
            "provider": "local",
            "model": "scripted-1",
            "phase": "running",
            "created_at": 1700000000,
        },
    )
    _started(handle, "child-1")
    _finished(handle, "child-1", status="failed", is_error=True)

    types = [record.type for record in activity_records(handle)]
    assert types == ["team/member", "subagent/descriptor", "subagent/catalog"]

    captured: list[Any] = []
    service = SimpleNamespace(refresh=captured.append)
    result = refresh_activity(service, handle)
    assert result is None
    assert len(captured) == 1
    records = captured[0].records
    assert [record.type for record in records] == ["team/member", "subagent/descriptor", "subagent/catalog"]
    member = records[0].data["member"]
    assert member["id"] == "member-1"
    assert member["phase"] == "running"
    descriptor = records[1].data
    assert descriptor["childId"] == "child-1"
    assert descriptor["label"] == "scout"
    assert refresh_activity(None, handle) is None
    assert team_activity_records(None) == []


async def test_failed_subagent_fact_projects_a_failed_run(tmp_path: Path) -> None:
    handle, _builder = _session(tmp_path)
    _started(handle, "child-1")
    _finished(handle, "child-1", status="failed", is_error=True)
    runs = subagent_runs(handle)
    assert runs[0].status == FAILED


def test_team_panel_renders_native_subagent_runs_without_changing_v4_rows(
    tmp_path: Path,
) -> None:
    handle, _builder = _session(tmp_path)
    _started(handle, "child-1")
    _finished(handle, "child-1")
    runs = subagent_runs(handle)
    rows = [normalize_subagent(run) for run in runs]
    screen = TeamPanelScreen(subagents=rows)
    assert screen.empty is False
    lines = screen.subagent_lines
    assert lines[0] == "subagents: 1 run(s)"
    assert "scout" in lines[1]
    assert "completed" in lines[1]
    assert "local/scripted-1" in lines[1]
    assert "usage not recorded" in lines[1]
    # v4 callers pass no subagents, so the panel stays exactly as before.
    assert TeamPanelScreen().subagent_lines == []
    assert TeamPanelScreen().empty is True
    assert subagent_lines([]) == []


class _PanelHost(PanelLayer):
    """Minimal host for panel-layer commands without a Textual app."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @property
    def conversation(self) -> Any:
        return SimpleNamespace(add_info=lambda _text: None, add_error=lambda _text: None)


def test_native_panel_reads_subagent_runs_from_session_facts(tmp_path: Path) -> None:
    handle, _builder = _session(tmp_path)
    _started(handle, "child-1")
    _finished(handle, "child-1")
    runtime = SimpleNamespace(
        native=True,
        session=handle,
        subagent=SimpleNamespace(usage=lambda child_id: None),
    )
    runs = _PanelHost(runtime)._native_subagent_runs()
    assert len(runs) == 1
    assert runs[0].child_id == "child-1"

    # The v4 runtime keeps the existing session-record activity projection.
    assert _PanelHost(SimpleNamespace(native=False, session=object()))._native_subagent_runs() == []
