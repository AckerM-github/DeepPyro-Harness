"""Goal tools: schema, output shape and the authorization ladder (M2a)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.goal import GoalError, GoalService
from deeppyro.tools.builtin.goal import (
    GOAL_TOOL_AGENT_REQUIRED,
    GOAL_TOOL_AUTHORITY_REQUIRED,
    GOAL_TOOL_BLOCK_THRESHOLD,
    GOAL_TOOL_DRIVER_REQUIRED,
    GOAL_TOOL_INVALID_UPDATE,
    GOAL_TOOL_RESUME_PAUSED,
    UPDATE_ACTIONS,
    CreateGoalTool,
    GetGoalTool,
    GoalTool,
    GoalToolError,
    UpdateGoalTool,
    is_root_agent,
)


class _Record:
    def __init__(self, type_: str, data: dict[str, Any]) -> None:
        self.type = type_
        self.data = data
        self.time = 0


class FakeSession:
    def __init__(self, session_id: str = "session-1", *, origin: str | None = None) -> None:
        self.session_id = session_id
        self.records: list[_Record] = []
        self.header = SimpleNamespace(origin=origin, parentSession=None, delegationDepth=0)

    def append(self, type_: str, data: dict[str, Any] | None = None) -> int:
        self.records.append(_Record(type_, dict(data or {})))
        return len(self.records)


class FakeAgent:
    def __init__(
        self,
        session: Any,
        *,
        human: bool = False,
        goal_round: dict[str, Any] | None = None,
        preset: str = "",
    ) -> None:
        self.session = session
        self.preset = preset
        self.turn = 1
        self.status = "running"
        self._human = human
        self._goal_round = goal_round

    def current_turn_human_input(self) -> bool:
        return self._human

    def current_turn_goal_round(self) -> dict[str, Any] | None:
        return self._goal_round

    def set_human(self, value: bool) -> None:
        self._human = value

    def set_goal_round(self, value: dict[str, Any] | None) -> None:
        self._goal_round = value


def make_context(
    tmp_path: Any,
    session: FakeSession,
    service: Any,
    *,
    agent: Any | None = None,
) -> ToolContext:
    def log(event: str, payload: dict[str, Any]) -> None:
        session.append(event, payload)

    return ToolContext(
        cwd=tmp_path,
        session_id=session.session_id,
        turn=1,
        step=0,
        call_id="call-goal",
        cancel=asyncio.Event(),
        log=log,
        services={"goal": service},
        agent=agent,
    )


def workspace(tmp_path: Any, *, human: bool = True) -> tuple[GoalService, FakeSession, FakeAgent, Any]:
    session = FakeSession()
    service = GoalService()
    agent = FakeAgent(session, human=human)
    return service, session, agent, make_context(tmp_path, session, service, agent=agent)


async def create_goal(agent: FakeAgent, service: GoalService, session: FakeSession, ctx: ToolContext):
    agent.set_human(True)
    result = await CreateGoalTool().run({"objective": "ship the plugin"}, ctx)
    return json.loads(result.text), service.view(session)


def code_of(exc: pytest.ExceptionInfo[BaseException]) -> str:
    return str(getattr(exc.value, "code", ""))


# -- schemas -------------------------------------------------------------
def test_get_goal_schema_is_empty_object() -> None:
    tool = GetGoalTool()
    assert tool.name == "get_goal"
    assert tool.parameters == {"type": "object", "properties": {}, "additionalProperties": False}
    assert "id and revision" in tool.description


def test_create_goal_schema_requires_the_objective() -> None:
    tool = CreateGoalTool()
    assert tool.name == "create_goal"
    assert tool.parameters["required"] == ["objective"]
    assert set(tool.parameters["properties"]) == {"objective", "max_goal_rounds"}
    assert tool.parameters["additionalProperties"] is False
    assert tool.description.startswith("Create a persisted goal")


def test_update_goal_schema_covers_every_action() -> None:
    tool = UpdateGoalTool()
    assert tool.name == "update_goal"
    assert tool.parameters["required"] == ["goal_id", "revision", "action"]
    assert tool.parameters["properties"]["action"]["enum"] == list(UPDATE_ACTIONS)
    assert set(tool.parameters["properties"]) == {
        "goal_id",
        "revision",
        "action",
        "objective",
        "max_goal_rounds",
        "blocked_reason",
    }


def test_deprecated_shell_is_marked_and_keeps_its_schema() -> None:
    tool = GoalTool()
    assert tool.name == "goal"
    assert tool.description.startswith("DEPRECATED: use get_goal/create_goal/update_goal")
    assert set(tool.parameters["properties"]) == {"text", "clear"}


# -- agent ladder --------------------------------------------------------
async def test_get_goal_without_an_agent_is_refused(tmp_path: Any) -> None:
    service, session, _agent, ctx = workspace(tmp_path)
    ctx.agent = None
    with pytest.raises(GoalToolError) as excinfo:
        await GetGoalTool().run({}, ctx)
    assert code_of(excinfo) == GOAL_TOOL_AGENT_REQUIRED


async def test_subagent_sessions_are_refused(tmp_path: Any) -> None:
    service = GoalService()
    session = FakeSession(origin="subagent")
    agent = FakeAgent(session, human=True)
    ctx = make_context(tmp_path, session, service, agent=agent)
    with pytest.raises(GoalToolError) as excinfo:
        await GetGoalTool().run({}, ctx)
    assert code_of(excinfo) == GOAL_TOOL_DRIVER_REQUIRED


def test_root_detection_uses_the_header_origin_then_the_preset() -> None:
    assert is_root_agent(FakeAgent(FakeSession())) is True
    assert is_root_agent(FakeAgent(FakeSession(origin="subagent"))) is False
    assert is_root_agent(FakeAgent(SimpleNamespace(session=None), preset="subagent/explore")) is False
    assert is_root_agent(FakeAgent(SimpleNamespace(session=None), preset="default")) is True


async def test_missing_goal_service_is_a_plain_tool_error(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    ctx.services = {}
    with pytest.raises(ToolError) as excinfo:
        await GetGoalTool().run({}, ctx)
    assert not isinstance(excinfo.value, GoalToolError)
    assert "not available" in str(excinfo.value)


# -- output shape --------------------------------------------------------
async def test_get_goal_reports_null_before_any_goal(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    result = await GetGoalTool().run({}, ctx)
    assert result.text == '{"goal":null}'
    assert json.loads(result.text) == {"goal": None}


async def test_create_goal_renders_the_shared_shape(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    payload, view = await create_goal(agent, service, session, ctx)
    assert view is not None
    assert set(payload) == {"goal", "activation"}
    assert payload["activation"] == "armed"
    goal = payload["goal"]
    assert set(goal) == {
        "id",
        "revision",
        "objective",
        "phase",
        "roundsStarted",
        "maxGoalRounds",
        "blockedReason",
    } or set(goal) == {"id", "revision", "objective", "phase", "roundsStarted", "maxGoalRounds"}
    assert goal["objective"] == "ship the plugin"
    assert goal["phase"] == "active"
    assert goal["revision"] == 1
    assert goal["roundsStarted"] == 0
    assert goal["maxGoalRounds"] == 256
    result = await GetGoalTool().run({}, ctx)
    assert result.text == json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# -- create authority ----------------------------------------------------
async def test_create_goal_requires_a_human_turn(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path, human=False)
    with pytest.raises(GoalToolError) as excinfo:
        await CreateGoalTool().run({"objective": "ship it"}, ctx)
    assert code_of(excinfo) == GOAL_TOOL_AUTHORITY_REQUIRED
    assert service.view(session) is None


@pytest.mark.parametrize("action", ["edit", "pause", "resume"])
async def test_mutating_actions_require_a_human_turn(tmp_path: Any, action: str) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    agent.set_human(False)
    agent.set_goal_round({"goalId": view.goal.id, "revision": view.goal.revision, "round": 1})
    with pytest.raises(GoalToolError) as excinfo:
        await UpdateGoalTool().run(
            {
                "goal_id": view.goal.id,
                "revision": view.goal.revision,
                "action": action,
                "objective": "other",
            },
            ctx,
        )
    assert code_of(excinfo) == GOAL_TOOL_AUTHORITY_REQUIRED


# -- update validation ---------------------------------------------------
@pytest.mark.parametrize(
    "args",
    [
        {"revision": 1, "action": "complete"},
        {"goal_id": "", "revision": 1, "action": "complete"},
        {"goal_id": "   ", "revision": 1, "action": "complete"},
        {"goal_id": "goal-1", "revision": 0, "action": "complete"},
        {"goal_id": "goal-1", "revision": -2, "action": "complete"},
        {"goal_id": "goal-1", "revision": "1", "action": "complete"},
        {"goal_id": "goal-1", "revision": True, "action": "complete"},
        {"goal_id": "goal-1", "revision": 1, "action": "block"},
        {"goal_id": "goal-1", "revision": 1},
    ],
)
async def test_update_goal_rejects_malformed_references(tmp_path: Any, args: dict[str, Any]) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    with pytest.raises(GoalToolError) as excinfo:
        await UpdateGoalTool().run(args, ctx)
    assert code_of(excinfo) == GOAL_TOOL_INVALID_UPDATE


async def test_stale_revision_is_refused_by_the_service(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    with pytest.raises(GoalError) as excinfo:
        await UpdateGoalTool().run(
            {"goal_id": view.goal.id, "revision": view.goal.revision + 1, "action": "complete"},
            ctx,
        )
    assert excinfo.value.code == "GOAL_STALE_REVISION"


async def test_edit_needs_at_least_one_field(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    with pytest.raises(GoalError) as excinfo:
        await UpdateGoalTool().run(
            {"goal_id": view.goal.id, "revision": view.goal.revision, "action": "edit"},
            ctx,
        )
    assert excinfo.value.code == "GOAL_INVALID_EDIT"


# -- resume / complete / blocked ----------------------------------------
async def test_resume_refuses_a_user_paused_goal(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    paused = json.loads(
        (
            await UpdateGoalTool().run(
                {"goal_id": view.goal.id, "revision": view.goal.revision, "action": "pause"}, ctx
            )
        ).text
    )
    assert paused["goal"]["phase"] == "paused"
    assert paused["activation"] == "disarmed"
    with pytest.raises(GoalToolError) as excinfo:
        await UpdateGoalTool().run(
            {"goal_id": view.goal.id, "revision": paused["goal"]["revision"], "action": "resume"},
            ctx,
        )
    assert code_of(excinfo) == GOAL_TOOL_RESUME_PAUSED


async def test_complete_is_allowed_inside_a_goal_round(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    agent.set_human(False)
    agent.set_goal_round({"goalId": view.goal.id, "revision": view.goal.revision, "round": 1})
    result = await UpdateGoalTool().run(
        {"goal_id": view.goal.id, "revision": view.goal.revision, "action": "complete"}, ctx
    )
    payload = json.loads(result.text)
    assert payload["goal"]["phase"] == "complete"
    assert payload["activation"] == "disarmed"


async def test_complete_needs_authority_outside_a_goal_round(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    agent.set_human(False)
    agent.set_goal_round({"goalId": view.goal.id, "revision": view.goal.revision + 1, "round": 1})
    with pytest.raises(GoalToolError) as excinfo:
        await UpdateGoalTool().run(
            {"goal_id": view.goal.id, "revision": view.goal.revision, "action": "complete"}, ctx
        )
    assert code_of(excinfo) == GOAL_TOOL_AUTHORITY_REQUIRED


@pytest.mark.parametrize("reason", [None, "", "   "])
async def test_blocked_requires_a_reason(tmp_path: Any, reason: str | None) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    args: dict[str, Any] = {
        "goal_id": view.goal.id,
        "revision": view.goal.revision,
        "action": "blocked",
    }
    if reason is not None:
        args["blocked_reason"] = reason
    with pytest.raises(GoalToolError) as excinfo:
        await UpdateGoalTool().run(args, ctx)
    assert code_of(excinfo) == GOAL_TOOL_INVALID_UPDATE


def add_goal_rounds(session: FakeSession, service: GoalService, view: Any, rounds: int) -> None:
    for round_number in range(1, rounds + 1):
        session.append(
            "user/message",
            {
                "turn": round_number + 1,
                "source": {
                    "kind": "goal",
                    "goalId": view.goal.id,
                    "revision": view.goal.revision,
                    "round": round_number,
                },
            },
        )
    service.invalidate(session)


async def test_blocked_is_refused_below_the_threshold(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    add_goal_rounds(session, service, view, 2)
    assert service.state(session).rounds_started == 2
    agent.set_human(False)
    agent.set_goal_round({"goalId": view.goal.id, "revision": view.goal.revision, "round": 3})
    with pytest.raises(GoalToolError) as excinfo:
        await UpdateGoalTool().run(
            {
                "goal_id": view.goal.id,
                "revision": view.goal.revision,
                "action": "blocked",
                "blocked_reason": "the registry is read-only",
            },
            ctx,
        )
    assert code_of(excinfo) == GOAL_TOOL_BLOCK_THRESHOLD


async def test_blocked_passes_at_the_threshold(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    add_goal_rounds(session, service, view, 3)
    agent.set_human(False)
    agent.set_goal_round({"goalId": view.goal.id, "revision": view.goal.revision, "round": 4})
    result = await UpdateGoalTool().run(
        {
            "goal_id": view.goal.id,
            "revision": view.goal.revision,
            "action": "blocked",
            "blocked_reason": "the registry is read-only",
        },
        ctx,
    )
    payload = json.loads(result.text)
    assert payload["goal"]["phase"] == "blocked"
    assert payload["goal"]["blockedReason"] == {
        "code": "model-reported",
        "message": "the registry is read-only",
    }


async def test_human_blocked_skips_the_threshold(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    result = await UpdateGoalTool().run(
        {
            "goal_id": view.goal.id,
            "revision": view.goal.revision,
            "action": "blocked",
            "blocked_reason": "the user asked to stop",
        },
        ctx,
    )
    assert json.loads(result.text)["goal"]["phase"] == "blocked"


async def test_ignored_fields_do_not_change_state(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    result = await UpdateGoalTool().run(
        {
            "goal_id": view.goal.id,
            "revision": view.goal.revision,
            "action": "pause",
            "objective": "something else",
            "blocked_reason": "ignored",
        },
        ctx,
    )
    payload = json.loads(result.text)
    assert payload["goal"]["objective"] == "ship the plugin"
    assert payload["goal"]["phase"] == "paused"


async def test_custom_threshold_is_honoured(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    _payload, view = await create_goal(agent, service, session, ctx)
    add_goal_rounds(session, service, view, 1)
    agent.set_human(False)
    agent.set_goal_round({"goalId": view.goal.id, "revision": view.goal.revision, "round": 2})
    tool = UpdateGoalTool(blocked_after=1)
    result = await tool.run(
        {
            "goal_id": view.goal.id,
            "revision": view.goal.revision,
            "action": "blocked",
            "blocked_reason": "still broken",
        },
        ctx,
    )
    assert json.loads(result.text)["goal"]["phase"] == "blocked"


# -- deprecated shell ----------------------------------------------------
async def test_deprecated_shell_reads_sets_and_clears(tmp_path: Any) -> None:
    service, session, agent, ctx = workspace(tmp_path)
    tool = GoalTool()
    assert (await tool.run({}, ctx)).text == "no goal"
    set_result = await tool.run({"text": "keep shipping"}, ctx)
    assert set_result.text == "goal set: keep shipping"
    assert json.loads((await GetGoalTool().run({}, ctx)).text)["goal"]["objective"] == "keep shipping"
    cleared = await tool.run({"clear": True}, ctx)
    assert cleared.text == "goal cleared"
    assert json.loads((await GetGoalTool().run({}, ctx)).text) == {"goal": None}
