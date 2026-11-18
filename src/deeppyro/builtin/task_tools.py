"""Durable task and handoff tools (KI-017): a task plugin, not a loop branch.

Tasks and handoffs are facts in the session log, exactly like goal changes or
team members: the service folds task.state, task.handoff and task.handoff.result
events (native schema 1) back into the six-state machine from
deeppyro.agent.task.  Reopening a session therefore restores unfinished work
without a second storage location.

task_handoff delegates one turn's context to the subagent service, writes the
handoff (summary, budget, open items, and the prompt) to the parent session
before the child runs, and copies the result and the child's real usage back
into the parent turn.  Token usage stays a payload fact and a trace fact under
native schema 1, never a session-log fact (schema 8.5).  Budget overruns fail
the task instead of silently overspending.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import Field

from deeppyro.agent.task import (
    DEFAULT_MAX_STEPS,
    Handoff,
    Task,
    TaskBudget,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    new_handoff,
    new_task,
    parse_status,
)
from deeppyro.api import Plugin, PluginConfig, PluginContext
from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.session.fold import fold
from deeppyro.session.schema import EventKind, NativeSession
from deeppyro.subagent.types import SubagentUsage
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import (
    NonEmptyStr,
    OptionalNonEmptyStr,
    ToolArgs,
    schema_enum,
    schema_number,
)

# -- persisted record types (log-only, ignorable for upstream-compatible readers) --
TASK_EVENT = "task/state"
HANDOFF_EVENT = "task/handoff"
HANDOFF_RESULT_EVENT = "task/handoff/result"
TASK_RECORD_VERSION = 1

#: v4 record type -> native schema 1 event kind for the same fact.
NATIVE_TASK_EVENTS: dict[str, EventKind] = {
    TASK_EVENT: EventKind.TASK_STATE,
    HANDOFF_EVENT: EventKind.TASK_HANDOFF,
    HANDOFF_RESULT_EVENT: EventKind.TASK_HANDOFF_RESULT,
}
_NATIVE_RECORD_TYPES: dict[EventKind, str] = {
    event_kind: record_type for record_type, event_kind in NATIVE_TASK_EVENTS.items()
}

# -- tool-layer error codes -------------------------------------------------
TASK_TOOL_AGENT_REQUIRED = "TASK_TOOL_AGENT_REQUIRED"
TASK_TOOL_SERVICE_REQUIRED = "TASK_TOOL_SERVICE_REQUIRED"
TASK_TOOL_SUBAGENT_REQUIRED = "TASK_TOOL_SUBAGENT_REQUIRED"
TASK_TOOL_INVALID_TASK = "TASK_TOOL_INVALID_TASK"
TASK_TOOL_INVALID_TRANSITION = "TASK_TOOL_INVALID_TRANSITION"
TASK_TOOL_INVALID_UPDATE = "TASK_TOOL_INVALID_UPDATE"
TASK_TOOL_BUDGET_EXCEEDED = "TASK_TOOL_BUDGET_EXCEEDED"
TASK_TOOL_HANDOFF_FAILED = "TASK_TOOL_HANDOFF_FAILED"

_AGENT_TEXT = "task tools require a live agent turn"
_BUDGET_TEXT = "the task's budget overrides the model's estimate"

#: 'task_update' actions mapped to their target status ('assign' is owner only).
UPDATE_ACTIONS = ("start", "block", "complete", "fail", "cancel", "assign")
_ACTION_TARGETS: dict[str, TaskStatus | None] = {
    "start": TaskStatus.IN_PROGRESS,
    "block": TaskStatus.BLOCKED,
    "complete": TaskStatus.COMPLETED,
    "fail": TaskStatus.FAILED,
    "cancel": TaskStatus.CANCELLED,
    "assign": None,
}

#: The six states exposed to the model as a status filter.
_STATUS_VALUES = tuple(status.value for status in TaskStatus)


class TaskToolError(ToolError):
    """A tool-layer refusal carrying a stable, diagnosable code."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# -- pure helpers -----------------------------------------------------------
def _session_key(session: Any) -> str:
    """Stable cache key for a session-like object."""
    if isinstance(session, str):
        return session
    if isinstance(session, NativeSession):
        return session.header.id
    for attribute in ("session_id", "sessionId"):
        value = getattr(session, attribute, None)
        if isinstance(value, str) and value:
            return value
    return f"object:{id(session)}"


def _fact_text(value: Any, name: str) -> str:
    if isinstance(value, str) and value:
        return value
    raise TaskValidationError(f"task fact requires a non-empty {name}")


def _native_task_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a v4 task/state payload to the native task.state shape."""
    task = payload.get("task")
    if not isinstance(task, Mapping):
        raise TaskValidationError("task fact requires a task object")
    action = payload.get("action")
    if action not in ("create", "update"):
        raise TaskValidationError(f"task fact has unknown action {action!r}")
    return {"action": action, "task": dict(task)}


def _native_handoff_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a v4 task/handoff payload to the native task.handoff shape.

    The v4 record embeds the parent usage snapshot in handler state; native
    schema 1 keeps token facts out of the log (schema 8.5), so it is dropped.
    """
    run_id = _fact_text(payload.get("runId"), "runId")
    raw = payload.get("handoff")
    if not isinstance(raw, Mapping):
        raise TaskValidationError("handoff fact requires a handoff object")
    state = raw.get("state")
    handoff_state = state if isinstance(state, Mapping) else {}
    data: dict[str, Any] = {
        "run_id": run_id,
        "from": _fact_text(raw.get("from"), "handoff from"),
        "to": _fact_text(raw.get("to"), "handoff to"),
        "summary": _fact_text(raw.get("summary"), "handoff summary"),
        "created_at": _fact_text(raw.get("created_at"), "handoff created_at"),
    }
    task_id = raw.get("task_id")
    if isinstance(task_id, str) and task_id:
        data["task_id"] = task_id
    prompt = handoff_state.get("prompt")
    if isinstance(prompt, str):
        data["prompt"] = prompt
    turn = handoff_state.get("turn")
    if isinstance(turn, int) and not isinstance(turn, bool):
        data["turn"] = turn
    call_id = handoff_state.get("callId")
    if isinstance(call_id, str) and call_id:
        data["call_id"] = call_id
    budget = handoff_state.get("budget")
    if isinstance(budget, Mapping):
        data["budget"] = dict(budget)
    open_items = raw.get("open_items")
    if isinstance(open_items, list):
        data["open_items"] = [item for item in open_items if isinstance(item, str)]
    return data


def _native_handoff_result_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate a v4 task/handoff/result payload to the native result shape."""
    run_id = _fact_text(payload.get("runId"), "runId")
    ok = payload.get("ok")
    if not isinstance(ok, bool):
        raise TaskValidationError("handoff result requires a boolean ok")
    result = payload.get("result")
    data: dict[str, Any] = {
        "run_id": run_id,
        "ok": ok,
        "result": result if isinstance(result, str) else "",
    }
    task_id = payload.get("taskId")
    if isinstance(task_id, str) and task_id:
        data["task_id"] = task_id
    error = payload.get("error")
    if isinstance(error, str) and error:
        data["error"] = error
    return data


def _native_payload(record_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Translate one v4 task fact payload to its native event data shape."""
    if record_type == TASK_EVENT:
        return _native_task_payload(payload)
    if record_type == HANDOFF_EVENT:
        return _native_handoff_payload(payload)
    return _native_handoff_result_payload(payload)


def _v4_task_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    return {"version": TASK_RECORD_VERSION, "action": data.get("action"), "task": data.get("task")}


def _v4_handoff_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for native_name, v4_name in (
        ("turn", "turn"),
        ("call_id", "callId"),
        ("budget", "budget"),
        ("prompt", "prompt"),
    ):
        if native_name in data:
            state[v4_name] = data[native_name]
    open_items = data.get("open_items")
    handoff = {
        "from": data.get("from"),
        "to": data.get("to"),
        "summary": data.get("summary"),
        "state": state,
        "open_items": list(open_items) if isinstance(open_items, list) else [],
        "created_at": data.get("created_at"),
        "task_id": data.get("task_id"),
    }
    return {"version": TASK_RECORD_VERSION, "runId": data.get("run_id"), "handoff": handoff}


def _v4_handoff_result_payload(data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": TASK_RECORD_VERSION,
        "runId": data.get("run_id"),
        "taskId": data.get("task_id"),
        "ok": data.get("ok"),
        "result": data.get("result"),
        "error": data.get("error", ""),
        "usage": None,
    }


def _record_payload(record_type: str, data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Normalize a native task fact to the v4 payload shape the fold understands."""
    if record_type == TASK_EVENT:
        return _v4_task_payload(data)
    if record_type == HANDOFF_EVENT:
        return _v4_handoff_payload(data)
    return _v4_handoff_result_payload(data)


def _facts_of(session: Any) -> list[tuple[str, Mapping[str, Any]]]:
    """Task facts in the session log, normalized to the v4 record payload shape."""
    if isinstance(session, NativeSession):
        facts: list[tuple[str, Mapping[str, Any]]] = []
        for event in session.events:
            kind = event.kind
            if kind is None:
                continue
            record_type = _NATIVE_RECORD_TYPES.get(kind)
            if record_type is None:
                continue
            facts.append((record_type, _record_payload(record_type, event.data)))
        return facts
    records = getattr(session, "records", None)
    if records is None:
        return []
    facts = []
    for record in records:
        record_type = getattr(record, "type", None)
        data = getattr(record, "data", None)
        if isinstance(record_type, str) and isinstance(data, Mapping):
            facts.append((record_type, data))
    return facts


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _count(value: Any) -> int:
    return _non_negative_int(value)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _message_text(message: Any) -> str:
    text_fn = getattr(message, "text", None)
    if callable(text_fn):
        try:
            rendered = text_fn()
        except Exception:  # noqa: BLE001 - a weird message costs only context
            rendered = ""
        if isinstance(rendered, str) and rendered:
            return rendered
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                candidate = part.get("text")
                if isinstance(candidate, str):
                    parts.append(candidate)
        return " ".join(parts)
    return ""


def _context_summary(session: Any, prompt: str) -> str:
    """A compact summary of the turn the handoff leaves behind."""
    messages: list[Any] = []
    if isinstance(session, NativeSession):
        try:
            messages = list(fold(session.header, session.events).messages)
        except Exception:  # noqa: BLE001 - fall back to the prompt
            messages = []
    else:
        derive: Any = getattr(session, "derive_messages", None)
        try:
            messages = list(derive()) if derive is not None else []
        except Exception:  # noqa: BLE001 - fall back to the prompt
            messages = []
    lines: list[str] = []
    for message in messages[-4:]:
        text = " ".join(_message_text(message).split())
        if not text:
            continue
        role = _text(getattr(message, "role", "")) or "message"
        lines.append(f"{role}: {text[:400]}")
    summary = " | ".join(lines) or prompt
    return summary[:2000]


def _session_usage(session: Any) -> dict[str, int]:
    usage_fn = getattr(session, "usage", None)
    raw: Any = None
    if callable(usage_fn):
        try:
            raw = usage_fn()
        except Exception:  # noqa: BLE001 - usage plumbing must never block
            raw = None
    if not isinstance(raw, Mapping):
        return {"input": 0, "output": 0, "cached": 0}
    return {
        "input": _count(raw.get("input")),
        "output": _count(raw.get("output")),
        "cached": _count(raw.get("cached")),
    }


def _budget_violation(budget: TaskBudget, usage: SubagentUsage | None) -> str:
    """Why 'usage' exceeded 'budget', or an empty string when it did not."""
    if usage is None:
        return ""
    if budget.max_tokens is not None and usage.total_tokens > budget.max_tokens:
        return f"{usage.total_tokens} tokens > max_tokens {budget.max_tokens}"
    if usage.steps > budget.max_steps:
        return f"{usage.steps} steps > max_steps {budget.max_steps}"
    if budget.max_seconds is not None:
        seconds = usage.latency_ms / 1000.0
        if seconds > budget.max_seconds:
            return f"{seconds:.3f}s > max_seconds {budget.max_seconds}"
    return ""


def _usage_of(result: Any) -> SubagentUsage | None:
    usage = getattr(result, "usage", None)
    return usage if isinstance(usage, SubagentUsage) else None


# -- the persisted projection ----------------------------------------------
@dataclass(slots=True)
class _TaskState:
    """Everything the session log says about tasks and handoffs."""

    tasks: dict[str, Task] = field(default_factory=dict)
    handoffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)


def _apply_task_fact(state: _TaskState, data: Mapping[str, Any]) -> None:
    raw = data.get("task")
    if not isinstance(raw, Mapping):
        return
    try:
        task = Task.from_dict(dict(raw))
    except TaskValidationError:  # a corrupted or future fact must not brick the log
        return
    state.tasks[task.id] = task


def _apply_handoff_fact(state: _TaskState, record_type: str, data: Mapping[str, Any]) -> None:
    run_id = data.get("runId")
    if not isinstance(run_id, str) or not run_id:
        return
    payload = dict(data)
    entry = state.handoffs.get(run_id)
    if entry is None:
        entry = {"run": payload, "result": None}
        state.handoffs[run_id] = entry
        state.order.append(run_id)
    if record_type == HANDOFF_RESULT_EVENT:
        entry["result"] = payload


class TaskService:
    """Fold task and handoff facts out of one session log.

    The service is deliberately storage-agnostic: callers hand it a native
    NativeSession with events and append (or a native session handle exposing
    the same read surface), so the same fold works for a reopened session read
    through the native store.
    """

    provide: ClassVar[str] = "task"

    def __init__(self) -> None:
        self._states: dict[str, tuple[int, _TaskState]] = {}

    # -- reading -----------------------------------------------------------
    def tasks(self, session: Any) -> tuple[Task, ...]:
        state = self._fold(session)
        return tuple(sorted(state.tasks.values(), key=lambda task: (task.created_at, task.id)))

    def unfinished(self, session: Any) -> tuple[Task, ...]:
        return tuple(task for task in self.tasks(session) if not task.is_terminal)

    def find(self, session: Any, task_id: Any) -> Task | None:
        if not isinstance(task_id, str) or not task_id:
            raise TaskValidationError("task id must be a non-empty string")
        state = self._fold(session)
        task = state.tasks.get(task_id)
        if task is not None:
            return task
        # Allow the short form models tend to copy; the canonical id stays strict.
        matches = [candidate for candidate in state.tasks if candidate.startswith(task_id)]
        if len(matches) == 1:
            return state.tasks[matches[0]]
        return None

    def get(self, session: Any, task_id: Any) -> Task:
        if isinstance(task_id, str) and task_id and not task_id.startswith("task_"):
            raise TaskValidationError(f"task id must match 'task_<16 hex>', got {task_id!r}")
        task = self.find(session, task_id)
        if task is None:
            raise TaskValidationError(f"unknown task {task_id!r}")
        return task

    def handoffs(self, session: Any) -> tuple[dict[str, Any], ...]:
        state = self._fold(session)
        return tuple(state.handoffs[run_id]["run"] for run_id in state.order)

    def handoff_result(self, session: Any, run_id: str) -> dict[str, Any] | None:
        state = self._fold(session)
        entry = state.handoffs.get(run_id)
        if entry is None:
            return None
        return entry["result"]

    def pending_handoffs(self, session: Any) -> tuple[dict[str, Any], ...]:
        state = self._fold(session)
        return tuple(
            state.handoffs[run_id]["run"]
            for run_id in state.order
            if state.handoffs[run_id]["result"] is None
        )

    # -- writing -----------------------------------------------------------
    def create(
        self,
        session: Any,
        objective: Any,
        *,
        owner: Any = None,
        parent: Any = None,
        budget: TaskBudget | None = None,
    ) -> Task:
        if budget is not None and not isinstance(budget, TaskBudget):
            raise TaskValidationError("budget must be a TaskBudget")
        if parent is not None:
            self.get(session, parent)
        task = new_task(objective, owner=owner, parent=parent, budget=budget)
        self._append(
            session, TASK_EVENT, {"version": TASK_RECORD_VERSION, "action": "create", "task": task.to_dict()}
        )
        return task

    def update(self, session: Any, task_id: Any, status: Any, *, owner: Any = None) -> Task:
        task = self.get(session, task_id)
        target = status if isinstance(status, TaskStatus) else parse_status(status)
        updated = task
        if owner is not None:
            updated = updated.assign(owner)
        if updated.status is not target:
            updated = updated.transition(target)
        elif updated is task:
            raise TaskStateError(f"task {task.id} is already {target.value}")
        self._append(
            session,
            TASK_EVENT,
            {"version": TASK_RECORD_VERSION, "action": "update", "task": updated.to_dict()},
        )
        return updated

    def assign(self, session: Any, task_id: Any, owner: Any) -> Task:
        task = self.get(session, task_id)
        updated = task.assign(owner)
        self._append(
            session,
            TASK_EVENT,
            {"version": TASK_RECORD_VERSION, "action": "update", "task": updated.to_dict()},
        )
        return updated

    def complete(self, session: Any, task_id: Any) -> Task:
        return self.update(session, task_id, TaskStatus.COMPLETED)

    def start_handoff(self, session: Any, handoff: Handoff, *, run_id: str) -> None:
        self._append(
            session,
            HANDOFF_EVENT,
            {"version": TASK_RECORD_VERSION, "runId": run_id, "handoff": handoff.to_dict()},
        )

    def finish_handoff(
        self,
        session: Any,
        *,
        run_id: str,
        task_id: str,
        ok: bool,
        result: str = "",
        error: str = "",
        usage: SubagentUsage | None = None,
    ) -> None:
        self._append(
            session,
            HANDOFF_RESULT_EVENT,
            {
                "version": TASK_RECORD_VERSION,
                "runId": run_id,
                "taskId": task_id,
                "ok": ok,
                "result": result,
                "error": error,
                "usage": None if usage is None else usage.to_dict(),
            },
        )

    # -- internals ---------------------------------------------------------
    def _append(self, session: Any, record_type: str, payload: dict[str, Any]) -> None:
        append = getattr(session, "append", None)
        if not callable(append):
            raise TaskValidationError("task service requires a session with append(); pass a session log")
        if isinstance(session, NativeSession):
            session.append(NATIVE_TASK_EVENTS[record_type], _native_payload(record_type, payload))
            return
        append(record_type, payload, ignorable=True)

    def _fold(self, session: Any) -> _TaskState:
        facts = _facts_of(session)
        key = _session_key(session)
        cached = self._states.get(key)
        if cached is not None and cached[0] == len(facts):
            return cached[1]
        state = _TaskState()
        for record_type, data in facts:
            if record_type == TASK_EVENT:
                _apply_task_fact(state, data)
            elif record_type in (HANDOFF_EVENT, HANDOFF_RESULT_EVENT):
                _apply_handoff_fact(state, record_type, data)
        self._states[key] = (len(facts), state)
        return state


# -- tool plumbing ----------------------------------------------------------
def _result(payload: dict[str, Any]) -> ToolResult:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(text=text, meta=payload)


def _agent_and_session(ctx: ToolContext) -> tuple[Any, Any]:
    agent = getattr(ctx, "agent", None)
    session = getattr(agent, "session", None) if agent is not None else None
    if session is None:
        # The native loop owns the NativeSession and exposes it as a service;
        # tool calls there have no v4 agent session to read from.
        session = (ctx.services or {}).get("session")
    if session is None:
        raise TaskToolError(_AGENT_TEXT, TASK_TOOL_AGENT_REQUIRED)
    return agent, session


def _resolve_service(service: TaskService | None, ctx: ToolContext) -> TaskService:
    if service is not None:
        return service
    candidate = (ctx.services or {}).get("task")
    if isinstance(candidate, TaskService):
        return candidate
    raise TaskToolError("task service is not available in this session", TASK_TOOL_SERVICE_REQUIRED)


def _domain_error(exc: Exception) -> TaskToolError:
    if isinstance(exc, TaskStateError):
        return TaskToolError(str(exc), TASK_TOOL_INVALID_TRANSITION)
    if isinstance(exc, TaskValidationError):
        return TaskToolError(str(exc), TASK_TOOL_INVALID_TASK)
    return TaskToolError(f"{type(exc).__name__}: {exc}", TASK_TOOL_INVALID_TASK)


def _task_owner(agent: Any, session: Any) -> str:
    if agent is not None:
        owner = getattr(agent, "agent_id", None) or getattr(agent, "name", None)
        if isinstance(owner, str) and owner:
            return owner
    session_id = getattr(session, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        session_id = getattr(getattr(session, "header", None), "id", None)
    if isinstance(session_id, str) and session_id:
        return session_id
    return "lead"


def _handoff_turn(agent: Any, ctx: ToolContext) -> int:
    turn = _int(getattr(agent, "turn", 0)) if agent is not None else 0
    return turn if turn > 0 else _int(ctx.turn)


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


# -- tools ------------------------------------------------------------------
class TaskCreateTool(BaseTool):
    """Create a durable task in the six-state machine."""

    name = "task_create"
    description = (
        "Create a durable task for this session. Tasks are persisted as session facts, "
        "move through pending/in_progress/blocked/completed/failed/cancelled, and survive "
        "resume or fork. Use task_update to change state and task_handoff to delegate one."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for task_create."""

        objective: NonEmptyStr = Field(description="What the task must accomplish, in one or two sentences.")
        parent: OptionalNonEmptyStr = Field(
            default=None, description="Optional parent task id (task_<16 hex>)."
        )
        owner: OptionalNonEmptyStr = Field(default=None, description="Optional owner for the task.")
        max_steps: int | None = Field(
            default=None,
            json_schema_extra=schema_number(),
            description="Optional step budget (positive integer, default 32).",
        )
        max_tokens: int | None = Field(
            default=None,
            json_schema_extra=schema_number(),
            description="Optional token budget; a handoff that exceeds it fails the task.",
        )
        max_seconds: float | None = Field(
            default=None,
            json_schema_extra=schema_number(),
            description="Optional wall-clock budget in seconds.",
        )

    def __init__(self, service: TaskService | None = None) -> None:
        self.service = service

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _agent, session = _agent_and_session(ctx)
        parsed = self.Args.parse(args)
        service = _resolve_service(self.service, ctx)
        budget = _budget_from_args(parsed)
        try:
            task = service.create(
                session,
                parsed.objective,
                owner=parsed.owner,
                parent=parsed.parent,
                budget=budget,
            )
        except (TaskValidationError, TaskStateError) as exc:
            raise _domain_error(exc) from None
        return _result({"task": task.to_dict(), "unfinished": len(service.unfinished(session))})


class TaskUpdateTool(BaseTool):
    """Move a task through the six-state machine."""

    name = "task_update"
    description = (
        "Update a durable task. Actions: start (pending/blocked -> in_progress), block, "
        "complete, fail, cancel, assign (owner only). Illegal transitions are refused with "
        "the allowed target states, and the task stays unchanged."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for task_update."""

        task_id: NonEmptyStr = Field(description="The task id (task_<16 hex>) or its unique prefix.")
        action: str = Field(
            description="The transition to apply.",
            json_schema_extra=schema_enum(*UPDATE_ACTIONS),
        )
        owner: OptionalNonEmptyStr = Field(default=None, description="New owner for action=assign.")
        reason: OptionalNonEmptyStr = Field(default=None, description="Optional note stored with the update.")

    def __init__(self, service: TaskService | None = None) -> None:
        self.service = service

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _agent, session = _agent_and_session(ctx)
        parsed = self.Args.parse(args)
        service = _resolve_service(self.service, ctx)
        action = _action_of(parsed.action)
        try:
            if action == "assign":
                if parsed.owner is None:
                    raise TaskToolError(
                        "owner is required for task_update action 'assign'", TASK_TOOL_INVALID_UPDATE
                    )
                task = service.assign(session, parsed.task_id, parsed.owner)
            else:
                target = _ACTION_TARGETS[action]
                if target is None:  # pragma: no cover - guarded by _action_of
                    raise TaskToolError(f"unknown task action {action!r}", TASK_TOOL_INVALID_UPDATE)
                task = service.update(session, parsed.task_id, target, owner=parsed.owner)
        except (TaskValidationError, TaskStateError) as exc:
            raise _domain_error(exc) from None
        payload: dict[str, Any] = {"task": task.to_dict(), "action": action}
        if parsed.reason is not None:
            payload["reason"] = parsed.reason
        return _result(payload)


class TaskCompleteTool(BaseTool):
    """Complete a durable task (the common transition)."""

    name = "task_complete"
    description = "Complete a durable task; equivalent to task_update action=complete."
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for task_complete."""

        task_id: NonEmptyStr = Field(description="The task id (task_<16 hex>) or its unique prefix.")
        reason: OptionalNonEmptyStr = Field(default=None, description="Optional completion note.")

    def __init__(self, service: TaskService | None = None) -> None:
        self.service = service

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _agent, session = _agent_and_session(ctx)
        parsed = self.Args.parse(args)
        service = _resolve_service(self.service, ctx)
        try:
            task = service.complete(session, parsed.task_id)
        except (TaskValidationError, TaskStateError) as exc:
            raise _domain_error(exc) from None
        payload: dict[str, Any] = {"task": task.to_dict()}
        if parsed.reason is not None:
            payload["reason"] = parsed.reason
        return _result(payload)


class TaskListTool(BaseTool):
    """List the session's durable tasks and their states."""

    name = "task_list"
    description = (
        "List durable tasks persisted in this session, with counts by state. Pass status "
        "to filter (pending, in_progress, blocked, completed, failed, cancelled)."
    )
    parallel_safe = True

    class Args(ToolArgs):
        """Arguments for task_list."""

        status: OptionalNonEmptyStr = Field(
            default=None,
            json_schema_extra=schema_enum(*_STATUS_VALUES),
            description="Optional state filter.",
        )

    def __init__(self, service: TaskService | None = None) -> None:
        self.service = service

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        _agent, session = _agent_and_session(ctx)
        parsed = self.Args.parse(args)
        service = _resolve_service(self.service, ctx)
        tasks = service.tasks(session)
        if parsed.status is not None:
            try:
                wanted = parse_status(parsed.status)
            except TaskStateError as exc:
                raise _domain_error(exc) from None
            tasks = tuple(task for task in tasks if task.status is wanted)
        counts = {status.value: 0 for status in TaskStatus}
        for task in tasks:
            counts[task.status.value] += 1
        return _result(
            {
                "tasks": [task.to_dict() for task in tasks],
                "counts": counts,
                "unfinished": sum(1 for task in tasks if not task.is_terminal),
            }
        )


class TaskHandoffTool(BaseTool):
    """Hand the current turn to a subagent and fold its result back."""

    name = "task_handoff"
    description = (
        "Hand a task to a subagent in the background: the parent turn's summary, the "
        "task budget and the open items are persisted as a handoff fact and passed to "
        "the subagent in its work packet, the subagent's reply and real token usage are "
        "written back into this turn, and the task advances to completed (or failed when "
        "the subagent errors or the budget is exceeded)."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for task_handoff."""

        prompt: NonEmptyStr = Field(description="The self-contained work packet for the subagent.")
        task_id: OptionalNonEmptyStr = Field(
            default=None,
            description="Task to run; when omitted a new task is created from this handoff.",
        )
        summary: OptionalNonEmptyStr = Field(
            default=None,
            description="Optional override for the handoff summary (defaults to the turn context).",
        )
        open_items: list[str] | None = Field(
            default=None,
            description="Optional open items the child should finish.",
        )

    def __init__(self, service: TaskService | None = None) -> None:
        self.service = service

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent, session = _agent_and_session(ctx)
        parsed = self.Args.parse(args)
        service = _resolve_service(self.service, ctx)
        subagent = (ctx.services or {}).get("subagent")
        run: Any = getattr(subagent, "run", None)
        if run is None:
            raise TaskToolError(
                "task_handoff requires the subagent service in this session",
                TASK_TOOL_SUBAGENT_REQUIRED,
            )
        try:
            if parsed.task_id is None:
                task = service.create(session, parsed.summary or parsed.prompt)
            else:
                task = service.get(session, parsed.task_id)
            if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                task = service.update(session, task.id, TaskStatus.IN_PROGRESS)
            elif task.status is not TaskStatus.IN_PROGRESS:
                raise TaskToolError(
                    f"task {task.id} is {task.status.value} and cannot start a handoff",
                    TASK_TOOL_INVALID_TRANSITION,
                )
        except (TaskValidationError, TaskStateError) as exc:
            raise _domain_error(exc) from None

        run_id = f"handoff-{uuid.uuid4().hex[:12]}"
        summary = parsed.summary or _context_summary(session, parsed.prompt)
        open_items = tuple(parsed.open_items or ())
        handoff = _build_handoff(
            _task_owner(agent, session),
            session,
            task,
            summary,
            turn=_handoff_turn(agent, ctx),
            run_id=run_id,
            prompt=parsed.prompt,
            open_items=open_items,
            call_id=ctx.call_id,
        )
        service.start_handoff(session, handoff, run_id=run_id)
        packet = _handoff_packet(parsed.prompt, summary, task.budget, _session_usage(session))
        try:
            result = await run(packet, description=f"handoff {task.id}")
        except asyncio.CancelledError:
            service.finish_handoff(
                session,
                run_id=run_id,
                task_id=task.id,
                ok=False,
                error="cancelled",
            )
            _fail_task(service, session, task.id)
            raise
        except Exception as exc:  # noqa: BLE001 - a broken child must not break the parent
            error = f"{type(exc).__name__}: {exc}"
            service.finish_handoff(session, run_id=run_id, task_id=task.id, ok=False, error=error)
            _fail_task(service, session, task.id)
            raise TaskToolError(f"task_handoff failed: {error}", TASK_TOOL_HANDOFF_FAILED) from None

        usage = _usage_of(result)
        is_error = bool(getattr(result, "is_error", False))
        error = _text(getattr(result, "error", ""))
        text = _text(getattr(result, "text", ""))
        service.finish_handoff(
            session,
            run_id=run_id,
            task_id=task.id,
            ok=not is_error,
            result=text,
            error=error,
            usage=usage,
        )
        if is_error:
            _fail_task(service, session, task.id)
            raise TaskToolError(
                f"task_handoff failed: {error or 'the subagent reported an error'}",
                TASK_TOOL_HANDOFF_FAILED,
            )
        violation = _budget_violation(task.budget, usage)
        if violation:
            _fail_task(service, session, task.id)
            raise TaskToolError(
                f"task {task.id} exceeded its budget: {violation} ({_BUDGET_TEXT})",
                TASK_TOOL_BUDGET_EXCEEDED,
            )
        try:
            completed = service.complete(session, task.id)
        except (TaskValidationError, TaskStateError) as exc:
            raise _domain_error(exc) from None
        return _result(
            {
                "task": completed.to_dict(),
                "handoff": handoff.to_dict(),
                "runId": run_id,
                "result": text,
                "usage": None if usage is None else usage.to_dict(),
                "openItems": list(open_items),
            }
        )


def _budget_from_args(parsed: Any) -> TaskBudget | None:
    if parsed.max_steps is None and parsed.max_tokens is None and parsed.max_seconds is None:
        return None
    return TaskBudget(
        max_steps=DEFAULT_MAX_STEPS if parsed.max_steps is None else parsed.max_steps,
        max_tokens=parsed.max_tokens,
        max_seconds=parsed.max_seconds,
    )


def _action_of(action: str) -> str:
    if action not in _ACTION_TARGETS:
        expected = ", ".join(UPDATE_ACTIONS)
        raise TaskToolError(
            f"unknown task action {action!r}; expected one of {expected}", TASK_TOOL_INVALID_UPDATE
        )
    return action


def _fail_task(service: TaskService, session: Any, task_id: str) -> None:
    with contextlib.suppress(TaskValidationError, TaskStateError):
        service.update(session, task_id, TaskStatus.FAILED)


def _handoff_packet(prompt: str, summary: str, budget: TaskBudget, usage: dict[str, int]) -> str:
    """The work packet for the child: the prompt plus the context the parent hands over."""
    context = {
        "summary": summary,
        "budget": budget.to_dict(),
        "parentUsage": usage,
    }
    encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prompt}\n\n[handoff context]\n{encoded}"


def _build_handoff(
    owner: str,
    session: Any,
    task: Task,
    summary: str,
    *,
    turn: int,
    run_id: str,
    prompt: str,
    open_items: tuple[str, ...],
    call_id: str,
) -> Handoff:
    state: dict[str, Any] = {
        "turn": turn,
        "callId": call_id,
        "usage": _session_usage(session),
        "budget": task.budget.to_dict(),
        "prompt": prompt,
    }
    return new_handoff(
        from_owner=owner,
        to_owner=f"subagent:{run_id}",
        summary=summary,
        state=state,
        open_items=open_items,
        task_id=task.id,
    )


def task_tools(service: TaskService | None = None) -> list[BaseTool]:
    """The tools the task plugin registers."""
    return [
        TaskCreateTool(service),
        TaskUpdateTool(service),
        TaskCompleteTool(service),
        TaskListTool(service),
        TaskHandoffTool(service),
    ]


class TaskPlugin(Plugin):
    """Provide the task service and its tools."""

    provide = "task"
    plugin_id = "task"
    version = "0.3.0"
    description = "durable task and handoff tools backed by the session log"
    inject: ClassVar[list[str]] = ["tools"]

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self.service = TaskService()

    def task_tools(self) -> list[BaseTool]:
        """The tools this plugin registers."""
        return task_tools(self.service)

    def setup(self) -> None:
        self.ctx.provide("task", self.service)
        self.ctx.register_tool(lambda _config, _workspace: self.task_tools())


__all__ = [
    "HANDOFF_EVENT",
    "HANDOFF_RESULT_EVENT",
    "NATIVE_TASK_EVENTS",
    "TASK_EVENT",
    "TASK_RECORD_VERSION",
    "TASK_TOOL_AGENT_REQUIRED",
    "TASK_TOOL_BUDGET_EXCEEDED",
    "TASK_TOOL_HANDOFF_FAILED",
    "TASK_TOOL_INVALID_TASK",
    "TASK_TOOL_INVALID_TRANSITION",
    "TASK_TOOL_INVALID_UPDATE",
    "TASK_TOOL_SERVICE_REQUIRED",
    "TASK_TOOL_SUBAGENT_REQUIRED",
    "UPDATE_ACTIONS",
    "TaskCompleteTool",
    "TaskCreateTool",
    "TaskHandoffTool",
    "TaskListTool",
    "TaskPlugin",
    "TaskService",
    "TaskToolError",
    "TaskUpdateTool",
    "task_tools",
]
