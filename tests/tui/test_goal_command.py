"""The `/goal` command: fixed rendering, verbs and human resume."""

from __future__ import annotations

from typing import Any

from deeppyro.goal import GoalService
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.session_view import GOAL_USAGE, goal_lines, goal_summary_line


class _Record:
    def __init__(self, type_: str, data: dict[str, Any], time: int = 0) -> None:
        self.type = type_
        self.data = data
        self.time = time


class _Session:
    def __init__(self, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.records: list[_Record] = []

    def append(self, type_: str, data: dict[str, Any] | None = None) -> int:
        self.records.append(_Record(type_, dict(data or {}), time=len(self.records) + 1))
        return len(self.records)


class _Clock:
    def __init__(self, start: int = 1_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 10
        return self.value


class _Conversation:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []

    def add_info(self, text: str) -> None:
        self.infos.append(text)

    def add_error(self, text: str) -> None:
        self.errors.append(text)


class _Runtime:
    def __init__(self, service: Any | None, session: Any | None) -> None:
        self.goal = service
        self.session = session


class _Host:
    def __init__(self, service: Any | None, session: Any | None) -> None:
        self.runtime = _Runtime(service, session)
        self.conversation = _Conversation()
        self.refreshes = 0

    def refresh_status(self) -> None:
        self.refreshes += 1


def _service() -> GoalService:
    service = GoalService()
    service.configure(clock=_Clock())
    return service


def _host() -> tuple[_Host, GoalService, _Session]:
    service = _service()
    session = _Session()
    return _Host(service, session), service, session


async def _run(host: _Host, argument: str = "") -> None:
    await DeepPyroApp.goal_command(host, argument)  # type: ignore[arg-type]


async def test_goal_without_a_goal_prints_the_usage_lines() -> None:
    host, _service_instance, _session = _host()

    await _run(host)

    assert host.conversation.infos == ["No goal is currently set.", GOAL_USAGE]
    assert host.conversation.errors == []


async def test_goal_renders_the_fixed_block_and_pause_hint() -> None:
    host, _service_instance, session = _host()

    await _run(host, "ship the release")

    assert host.conversation.infos == [
        "Goal: ship the release",
        "Status: active",
        "Rounds: 0/256",
        "Activation: armed",
        "Commands: /goal edit <objective>, /goal pause, /goal clear",
    ]
    assert host.refreshes == 1
    assert _service_instance.view(session) is not None


async def test_goal_renders_blocked_reason_and_resume_hint() -> None:
    host, service, session = _host()
    created = await service.create(session, "ship it")
    await service.block(session, created.ref, "waiting for the API key", code="missing-key")

    await _run(host)

    assert "Status: blocked" in host.conversation.infos
    assert "Activation: disarmed" in host.conversation.infos
    assert "Blocker: missing-key: waiting for the API key" in host.conversation.infos
    assert "Commands: /goal edit <objective>, /goal resume, /goal clear" in host.conversation.infos


async def test_goal_pause_and_resume_arm_the_goal() -> None:
    host, service, session = _host()
    await _run(host, "ship it")

    await _run(host, "pause")
    assert service.view(session).goal.phase == "paused"  # type: ignore[union-attr]
    assert service.activation(session) == "disarmed"
    assert "Commands: /goal edit <objective>, /goal resume, /goal clear" in host.conversation.infos

    await _run(host, "resume")
    assert service.view(session).goal.phase == "active"  # type: ignore[union-attr]
    assert service.activation(session) == "armed"


async def test_goal_edit_keeps_the_phase_and_replaces_the_objective() -> None:
    host, service, session = _host()
    await _run(host, "first")
    await _run(host, "pause")

    await _run(host, "edit second")

    view = service.view(session)
    assert view is not None
    assert view.goal.objective == "second"
    assert view.goal.phase == "paused"
    assert "Goal: second" in host.conversation.infos


async def test_goal_complete_then_create_replaces_the_old_goal() -> None:
    host, service, session = _host()
    await _run(host, "first")

    await _run(host, "complete")
    assert service.view(session).goal.phase == "complete"  # type: ignore[union-attr]
    assert "Commands: /goal <objective>, /goal clear" in host.conversation.infos

    await _run(host, "second")
    view = service.view(session)
    assert view is not None
    assert view.goal.phase == "active"
    assert view.goal.objective == "second"


async def test_goal_clear_reports_the_cleared_state() -> None:
    host, service, session = _host()
    await _run(host, "first")

    await _run(host, "clear")

    assert host.conversation.infos[-1] == "Goal cleared."
    assert service.view(session) is None


async def test_goal_edit_without_a_body_shows_usage() -> None:
    host, service, session = _host()
    await service.create(session, "first")

    await _run(host, "edit")

    assert host.conversation.infos == [GOAL_USAGE]


async def test_goal_create_while_a_live_goal_exists_reports_the_phase() -> None:
    host, service, session = _host()
    await service.create(session, "first")

    await _run(host, "second")

    assert host.conversation.errors == [
        (
            "A goal is already active. Use /goal edit <objective> to change it "
            "or /goal clear before replacing it."
        )
    ]


async def test_goal_without_a_service_reports_an_error() -> None:
    host = _Host(None, _Session())

    await _run(host)

    assert host.conversation.errors == ["goal service is not available in this session"]


async def test_goal_without_a_session_reports_an_error() -> None:
    service = _service()
    host = _Host(service, None)

    await _run(host)

    assert host.conversation.errors == ["no active session"]


def test_goal_render_helpers_handle_the_empty_state() -> None:
    assert goal_lines(None) == ["No goal is currently set.", GOAL_USAGE]
    assert goal_summary_line(None) == ""
