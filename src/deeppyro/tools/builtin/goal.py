"""Goal tools (M2a): ``get_goal`` / ``create_goal`` / ``update_goal`` + the shell.

The three tools share one output shape and one authorization ladder; the contract
lives in ``docs/spec/goal.md`` section 5.  The old single ``goal`` tool stays for
one minor release as a deprecated forwarding shell.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import ToolArgs, schema_enum, schema_number

# -- tool-layer error codes (docs/spec/goal.md section 5.3) ---------------
GOAL_TOOL_AGENT_REQUIRED = "GOAL_TOOL_AGENT_REQUIRED"
GOAL_TOOL_AUTHORITY_REQUIRED = "GOAL_TOOL_AUTHORITY_REQUIRED"
GOAL_TOOL_BLOCK_THRESHOLD = "GOAL_TOOL_BLOCK_THRESHOLD"
GOAL_TOOL_DRIVER_REQUIRED = "GOAL_TOOL_DRIVER_REQUIRED"
GOAL_TOOL_INVALID_UPDATE = "GOAL_TOOL_INVALID_UPDATE"
GOAL_TOOL_RESUME_PAUSED = "GOAL_TOOL_RESUME_PAUSED"

#: Actions ``update_goal`` accepts, in contract spelling (``blocked``, not ``block``).
UPDATE_ACTIONS = ("edit", "pause", "resume", "complete", "blocked")
#: Default for ``[goal] blockedAfterConsecutiveRounds``.
DEFAULT_BLOCKED_AFTER_CONSECUTIVE_ROUNDS = 3

_AUTHORITY_TEXT = "this goal operation requires a direct human turn on a top-level agent"
_AGENT_TEXT = "goal tools require a live agent turn"


class GoalToolError(ToolError):
    """A tool-layer refusal carrying the upstream code from the spec."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# -- shared plumbing ------------------------------------------------------
def is_root_agent(agent: Any) -> bool:
    """Is ``agent`` a top-level agent rather than a subagent?

    The native subagent and team managers stamp every child session with
    ``origin="subagent"`` (plus a parent id and ``delegation_depth=1``), while a
    user fork keeps the source origin but bumps the depth.  The header origin is
    therefore the marker; the parent link alone would also reject forked
    top-level sessions.  Without a reachable header we fall back to the preset
    id, which is how subagent presets are named in practice.
    """
    header = getattr(getattr(agent, "session", None), "header", None)
    origin = getattr(header, "origin", None)
    if origin is not None:
        return origin != "subagent"
    preset = str(getattr(agent, "preset", "") or "")
    return not preset.startswith("subagent")


def goal_service(ctx: ToolContext) -> Any:
    service = (ctx.services or {}).get("goal")
    if service is None:
        raise ToolError("goal is not available in this session")
    return service


def agent_and_session(ctx: ToolContext) -> tuple[Any, Any]:
    """First two rungs of the ladder: a live agent on a top-level session."""
    agent = getattr(ctx, "agent", None)
    session = getattr(agent, "session", None) if agent is not None else None
    if agent is None or session is None:
        raise GoalToolError(_AGENT_TEXT, GOAL_TOOL_AGENT_REQUIRED)
    if not is_root_agent(agent):
        raise GoalToolError("goal tools only run on a top-level agent", GOAL_TOOL_DRIVER_REQUIRED)
    return agent, session


def _human_turn(agent: Any) -> bool:
    check = getattr(agent, "current_turn_human_input", None)
    return bool(check()) if callable(check) else False


def _goal_round(agent: Any) -> dict[str, Any] | None:
    check = getattr(agent, "current_turn_goal_round", None)
    value = check() if callable(check) else None
    return value if isinstance(value, dict) else None


def _round_matches(round_info: dict[str, Any] | None, goal_id: str, revision: int) -> bool:
    if not round_info:
        return False
    return round_info.get("goalId") == goal_id and round_info.get("revision") == revision


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _payload(service: Any, session: Any) -> dict[str, Any]:
    """The shared tool output: ``{goal: null}`` or ``{goal: {...}, activation}``."""
    view = service.view(session)
    if view is None:
        return {"goal": None}
    goal = view.goal
    data: dict[str, Any] = {
        "id": goal.id,
        "revision": goal.revision,
        "objective": goal.objective,
        "phase": goal.phase,
        "roundsStarted": view.rounds_started,
        "maxGoalRounds": goal.max_goal_rounds,
    }
    if goal.blocked_reason is not None:
        data["blockedReason"] = goal.blocked_reason.to_dict()
    return {"goal": data, "activation": view.activation}


def _result(payload: dict[str, Any]) -> ToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(text=text, meta=payload)


# -- the three tools ------------------------------------------------------
class GetGoalTool(BaseTool):
    """Read the current goal plus the id/revision that ``update_goal`` needs."""

    name = "get_goal"
    description = "Read the current session goal, including the id and revision that update_goal requires."
    parallel_safe = True

    class Args(ToolArgs):
        """get_goal takes no arguments."""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _agent, session = agent_and_session(ctx)
        service = goal_service(ctx)
        return _result(_payload(service, session))


class CreateGoalTool(BaseTool):
    """Create the persisted goal that drives automatic continuation rounds."""

    name = "create_goal"
    description = (
        "Create a persisted goal that keeps this session working across automatic "
        "continuation rounds. Use it when the direct human request is a long-running "
        'objective, even if the user did not say "goal"; not for single-turn work.'
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for create_goal."""

        objective: str = Field(
            description="The objective to keep working towards, in one or two sentences.",
        )
        max_goal_rounds: int | None = Field(
            default=None,
            json_schema_extra=schema_number(),
            description="Optional cap on automatic continuation rounds (positive integer).",
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent, session = agent_and_session(ctx)
        if not _human_turn(agent):
            raise GoalToolError(_AUTHORITY_TEXT, GOAL_TOOL_AUTHORITY_REQUIRED)
        service = goal_service(ctx)
        await service.create(
            session,
            args.get("objective"),
            max_goal_rounds=args.get("max_goal_rounds"),
        )
        return _result(_payload(service, session))


class UpdateGoalTool(BaseTool):
    """Move the current goal through edit/pause/resume/complete/blocked."""

    name = "update_goal"
    description = (
        "Update the current session goal. Pass the id and revision from get_goal; "
        "edit, pause and resume need a direct human turn, complete and blocked are "
        "also allowed inside a goal round."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for update_goal; the goal ladder validates their meaning."""

        goal_id: str = Field(description="The goal id from get_goal.")
        revision: int = Field(
            json_schema_extra=schema_number(),
            description="The goal revision from get_goal.",
        )
        action: str = Field(
            json_schema_extra=schema_enum(*UPDATE_ACTIONS),
            description="What to do with the goal.",
        )
        objective: str | None = Field(default=None, description="New objective, only for action=edit.")
        max_goal_rounds: int | None = Field(
            default=None,
            json_schema_extra=schema_number(),
            description="New round cap, only for action=edit.",
        )
        blocked_reason: str | None = Field(
            default=None,
            description="The concrete blocking condition, only for action=blocked.",
        )

    def __init__(self, *, blocked_after: int = DEFAULT_BLOCKED_AFTER_CONSECUTIVE_ROUNDS) -> None:
        self.blocked_after = blocked_after

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action, goal_id, revision = self._validate(args)
        agent, session = agent_and_session(ctx)
        service = goal_service(ctx)
        ref = _ref(goal_id, revision)

        if action in ("edit", "pause", "resume") and not _human_turn(agent):
            raise GoalToolError(_AUTHORITY_TEXT, GOAL_TOOL_AUTHORITY_REQUIRED)

        if action == "resume":
            view = service.view(session)
            if view is not None and view.goal.phase == "paused":
                raise GoalToolError(
                    f'goal "{goal_id}" is paused by the user; resume it with /goal resume',
                    GOAL_TOOL_RESUME_PAUSED,
                )

        rounds = _goal_round(agent)
        round_authorized = _round_matches(rounds, goal_id, revision)
        if action in ("complete", "blocked") and not (_human_turn(agent) or round_authorized):
            raise GoalToolError(_AUTHORITY_TEXT, GOAL_TOOL_AUTHORITY_REQUIRED)

        reason: str | None = None
        if action == "blocked":
            reason = _blocked_reason(args)
            if round_authorized and service.state(session).rounds_started < self.blocked_after:
                started = service.state(session).rounds_started
                raise GoalToolError(
                    "blocked needs the same condition to persist for at least "
                    f"{self.blocked_after} consecutive goal rounds (started {started})",
                    GOAL_TOOL_BLOCK_THRESHOLD,
                )

        if action == "edit":
            await service.edit(session, ref, **_edits(args))
        elif action == "pause":
            await service.pause(session, ref)
        elif action == "resume":
            await service.resume(session, ref)
        elif action == "complete":
            await service.complete(session, ref)
        else:
            assert reason is not None
            await service.block(session, ref, reason, code="model-reported")
        if action in ("complete", "blocked") and round_authorized and ctx.conclude_turn is not None:
            # upstream defers a closing message into the model context; v1 has no such
            # channel, so the documented downgrade is to end the turn here.
            conclusion: dict[str, Any] = {"kind": "completed"}
            if action == "blocked":
                conclusion = {"kind": "blocked", "reason": {"kind": "goal", "reason": "model-reported"}}
            ctx.conclude_turn(conclusion)
        return _result(_payload(service, session))

    @staticmethod
    def _validate(args: dict[str, Any]) -> tuple[str, str, int]:
        raw_id = args.get("goal_id")
        goal_id = raw_id.strip() if isinstance(raw_id, str) else ""
        revision = _positive_int(args.get("revision"))
        action = args.get("action")
        if not goal_id or revision is None or action not in UPDATE_ACTIONS:
            raise GoalToolError(
                "update_goal needs a non-empty goal_id, a positive integer revision "
                f"and one of {', '.join(UPDATE_ACTIONS)}",
                GOAL_TOOL_INVALID_UPDATE,
            )
        return str(action), goal_id, revision


def _ref(goal_id: str, revision: int) -> Any:
    from deeppyro.goal import GoalRef

    return GoalRef(goal_id, revision)


def _edits(args: dict[str, Any]) -> dict[str, Any]:
    """Only the two fields ``edit`` may write; everything else is ignored."""
    payload: dict[str, Any] = {}
    objective = args.get("objective")
    if objective is not None:
        payload["objective"] = objective
    rounds = args.get("max_goal_rounds")
    if rounds is not None:
        payload["max_goal_rounds"] = rounds
    return payload


def _blocked_reason(args: dict[str, Any]) -> str:
    raw = args.get("blocked_reason")
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise GoalToolError(
            "blocked_reason must describe the concrete blocking condition",
            GOAL_TOOL_INVALID_UPDATE,
        )
    return text


class GoalTool(BaseTool):
    """DEPRECATED shell: read, set or clear the goal through the goal service."""

    name = "goal"
    description = (
        "DEPRECATED: use get_goal/create_goal/update_goal. Read or update the goal of "
        "this session. Call it with no arguments to read, with text to set a short goal "
        "(one or two sentences), or with clear=true to drop it. The goal is recorded in "
        "the session log and shown in every later turn."
    )
    parallel_safe = True

    class Args(ToolArgs):
        """Arguments for the deprecated goal shell."""

        text: str | None = Field(default=None, description="The new goal. Omit to read the current one.")
        clear: bool | None = Field(default=None, description="Drop the current goal instead of setting one.")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("goal")
        if service is None:
            raise ToolError("goal is not available in this session")
        session = _legacy_session(ctx)
        if args.get("clear"):
            await service.clear(session)
            return ToolResult(text="goal cleared", meta={"goal": None})
        text = args.get("text")
        if text is None:
            goal = service.get(session)
            return ToolResult(text=goal or "no goal", meta={"goal": goal})
        goal = await service.set(session, str(text))
        return ToolResult(text=f"goal set: {goal}", meta={"goal": goal})


def _legacy_session(ctx: ToolContext) -> Any:
    """The shell predates ``ctx.agent``; keep working with a bare session id."""
    agent = getattr(ctx, "agent", None)
    session = getattr(agent, "session", None) if agent is not None else None
    return session if session is not None else ctx.session_id


__all__ = [
    "DEFAULT_BLOCKED_AFTER_CONSECUTIVE_ROUNDS",
    "GOAL_TOOL_AGENT_REQUIRED",
    "GOAL_TOOL_AUTHORITY_REQUIRED",
    "GOAL_TOOL_BLOCK_THRESHOLD",
    "GOAL_TOOL_DRIVER_REQUIRED",
    "GOAL_TOOL_INVALID_UPDATE",
    "GOAL_TOOL_RESUME_PAUSED",
    "UPDATE_ACTIONS",
    "CreateGoalTool",
    "GetGoalTool",
    "GoalTool",
    "GoalToolError",
    "UpdateGoalTool",
]
