"""Pure single-pass fold from schema 1 events to a session view.

The fold is deliberately inert: it reads no clock, performs no I/O and never
mutates its inputs. Every derived value comes from the header or the event log,
so replaying the same bytes always produces the same view.

The only tolerated divergence from the log is the dangling tool-call repair in
section 7.4 of docs/native/schema.md. An interrupted turn may leave a
tool.call without a tool.result; the view gains a deterministic synthetic tool
message and an issue, while the log itself is never rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from deeppyro.core.messages import (
    Message,
    MessageValidationError,
    ToolCallPart,
    missing_tool_result_message,
    replace_content,
)
from deeppyro.goal.types import BlockedReason, GoalSnapshot
from deeppyro.session.errors import (
    MessageError,
    MessageRefError,
    OrderingError,
    ToolPairingError,
)
from deeppyro.session.schema import Event, SessionHeader

PLUGIN_PREFIX = "plugin."


@dataclass(frozen=True, slots=True)
class ModelSelection:
    """The provider/model pair selected by the last model.selected event."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class PresetSelection:
    """The preset and mode selected by the last preset.selected event."""

    preset: str
    mode: str
    revision: int | None = None
    previous: str | None = None


@dataclass(frozen=True, slots=True)
class TodoItem:
    content: str
    status: str


@dataclass(frozen=True, slots=True)
class Deliverable:
    path: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One approval request still waiting for a decision."""

    approval_id: str
    tool_call_id: str
    tool_name: str
    reason: str | None
    requested_seq: int
    requested_ts: int
    outcome: str | None = None
    decided_seq: int | None = None


@dataclass(frozen=True, slots=True)
class FoldIssue:
    """A non-fatal derivation note; only section 7.4 repair creates these."""

    code: str
    message: str
    seq: int
    call_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskFact:
    """The last-wins task snapshot written by the task plugin."""

    task_id: str
    objective: str
    status: str
    owner: str | None
    created_at: str
    seq: int


@dataclass(frozen=True, slots=True)
class HandoffFact:
    """One multi-agent handoff and its optional result, folded from task events."""

    run_id: str
    from_owner: str
    to_owner: str
    summary: str
    task_id: str | None
    created_at: str
    seq: int
    open_items: tuple[str, ...] = ()
    result_ok: bool | None = None
    result: str | None = None
    error: str | None = None
    result_seq: int | None = None


@dataclass(frozen=True, slots=True)
class SessionState:
    """Last-wins state projected from state events."""

    model: ModelSelection | None = None
    preset: PresetSelection | None = None
    permission_policy: str | None = None
    sandbox_mode: str | None = None
    cwd: str | None = None
    title: str | None = None
    title_source: str | None = None
    goal: GoalSnapshot | None = None
    rounds_started: int = 0
    plan_mode: str | None = None
    todos: tuple[TodoItem, ...] = ()
    deliverables: tuple[Deliverable, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionView:
    """The deterministic projection of one schema 1 session."""

    header: SessionHeader
    messages: tuple[Message, ...]
    system: Message | None
    state: SessionState
    pending_approvals: tuple[ApprovalRecord, ...]
    last_seq: int
    last_message_id: str | None
    plugin_events: tuple[Event, ...]
    issues: tuple[FoldIssue, ...]
    tasks: tuple[TaskFact, ...] = ()
    handoffs: tuple[HandoffFact, ...] = ()


@dataclass(slots=True)
class _CallRecord:
    call_id: str
    turn_id: str
    step_id: str
    assistant_seq: int
    assistant_message_id: str
    part: ToolCallPart
    call_seq: int | None = None
    call_ts: int | None = None
    result_seq: int | None = None
    result_ts: int | None = None


class _Folder:
    def __init__(self, header: SessionHeader, events: tuple[Event, ...]) -> None:
        self.header = header
        self.events = events
        self.messages: list[Message] = []
        self.by_id: dict[str, int] = {}
        self.system: Message | None = None
        self.state = SessionState(cwd=header.cwd, preset=PresetSelection(header.preset, "default"))
        self.pending: dict[str, ApprovalRecord] = {}
        self.approvals_seen: set[str] = set()
        self.plugin_events: list[Event] = []
        self.issues: list[FoldIssue] = []
        self.calls: dict[str, _CallRecord] = {}
        self.turn_calls: dict[str, list[str]] = {}
        self.covered: set[str] = set()
        self.current_turn: str | None = None
        self.tasks: dict[str, TaskFact] = {}
        self.handoffs: dict[str, HandoffFact] = {}

    def run(self) -> SessionView:
        for event in self.events:
            self._apply(event)
        if self.current_turn is not None:
            self._finish_turn(self.current_turn, "open", self._last_seq(), self._last_ts())
        last_message_id: str | None = None
        if self.messages:
            last_message_id = self.messages[-1].id
        elif self.system is not None:
            last_message_id = self.system.id
        return SessionView(
            header=self.header,
            messages=tuple(self.messages),
            system=self.system,
            state=self.state,
            pending_approvals=tuple(sorted(self.pending.values(), key=lambda record: record.requested_seq)),
            last_seq=self._last_seq(),
            last_message_id=last_message_id,
            plugin_events=tuple(self.plugin_events),
            issues=tuple(self.issues),
            tasks=tuple(self.tasks.values()),
            handoffs=tuple(self.handoffs.values()),
        )

    def _last_seq(self) -> int:
        return self.events[-1].seq if self.events else 0

    def _last_ts(self) -> int:
        return self.events[-1].ts if self.events else self.header.created_at

    def _apply(self, event: Event) -> None:
        kind = event.type
        if kind.startswith(PLUGIN_PREFIX):
            self.plugin_events.append(event)
            return
        if kind == "turn.started":
            self.current_turn = event.turn_id
            return
        if kind == "turn.ended":
            reason = event.data["reason"]
            turn_id = _require_turn(event)
            self.current_turn = None
            self._finish_turn(turn_id, reason["kind"], event.seq, event.ts)
            return
        if kind == "step.started" or kind == "step.ended":
            return
        if kind == "user.message" or kind == "developer.message":
            self._append_message(self._message(event), event.seq)
            return
        if kind == "system.message":
            self.system = self._message(event)
            return
        if kind == "assistant.message":
            message = self._message(event)
            self._append_message(message, event.seq)
            self._register_calls(message, event)
            return
        if kind == "tool.call":
            self._tool_call(event)
            return
        if kind == "tool.result":
            self._tool_result(event)
            return
        if kind == "message.replaced":
            self._replace_message(event)
            return
        if kind == "compaction.done":
            self._compaction(event)
            return
        if kind == "approval.requested":
            self._approval_requested(event)
            return
        if kind == "approval.decided":
            self._approval_decided(event)
            return
        if kind == "model.selected":
            self.state = replace(
                self.state,
                model=ModelSelection(event.data["provider"], event.data["model"]),
            )
            return
        if kind == "preset.selected":
            self.state = replace(
                self.state,
                preset=PresetSelection(
                    event.data["preset"],
                    event.data["mode"],
                    event.data.get("revision"),
                    event.data.get("previous"),
                ),
            )
            return
        if kind == "permission.policy":
            self.state = replace(self.state, permission_policy=event.data["policy"])
            return
        if kind == "sandbox.mode":
            self.state = replace(self.state, sandbox_mode=event.data["mode"])
            return
        if kind == "workspace.cwd_changed":
            self.state = replace(self.state, cwd=event.data["cwd"])
            return
        if kind == "session.title":
            self.state = replace(
                self.state,
                title=event.data["title"],
                title_source=event.data["source"],
            )
            return
        if kind == "goal.changed":
            self.state = replace(
                self.state,
                goal=_goal_from_data(event.data["goal"]),
                rounds_started=event.data["rounds_started"],
            )
            return
        if kind == "plan.mode":
            self.state = replace(self.state, plan_mode=event.data["mode"])
            return
        if kind == "todo.written":
            self.state = replace(
                self.state,
                todos=tuple(TodoItem(item["content"], item["status"]) for item in event.data["todos"]),
            )
            return
        if kind == "deliverable.presented":
            self.state = replace(
                self.state,
                deliverables=tuple(
                    Deliverable(item["path"], item.get("description")) for item in event.data["files"]
                ),
            )
            return
        if kind == "task.state":
            self._task_state(event)
            return
        if kind == "task.handoff":
            self._task_handoff(event)
            return
        if kind == "task.handoff.result":
            self._task_handoff_result(event)
            return

    def _message(self, event: Event, key: str = "message") -> Message:
        raw = event.data[key]
        if isinstance(raw, Message):
            return raw
        try:
            return Message.from_dict(raw)
        except MessageValidationError as exc:
            raise MessageError(f"event {event.type!r} at seq {event.seq} message is invalid: {exc}") from exc

    def _append_message(self, message: Message, seq: int) -> None:
        if message.id in self.by_id:
            raise MessageError(f"event message at seq {seq} is invalid: duplicate message id {message.id!r}")
        self.by_id[message.id] = len(self.messages)
        self.messages.append(message)

    def _replace_message(self, event: Event) -> None:
        message_id = event.data["message_id"]
        replacement = self._message(event)
        index = self.by_id.get(message_id)
        if index is not None:
            old = self.messages[index]
        elif self.system is not None and self.system.id == message_id:
            old = self.system
        else:
            raise MessageRefError(
                f"event {event.type!r} at seq {event.seq} references unknown message id {message_id!r}"
            )
        if (
            replacement.role != old.role
            or replacement.created_at != old.created_at
            or replacement.source != old.source
            or replacement.tool_call_id != old.tool_call_id
        ):
            raise MessageError(
                f"event {event.type!r} at seq {event.seq} message is invalid: "
                "message.replaced may only change content and is_error"
            )
        updated = replace_content(old, replacement.content, is_error=replacement.is_error)
        if index is not None:
            self.messages[index] = updated
        else:
            self.system = updated

    def _compaction(self, event: Event) -> None:
        covers = event.data["covers"]
        summary = self._message(event)
        if len(set(covers)) != len(covers):
            raise MessageRefError(
                f"compaction at seq {event.seq} covers a non-contiguous range: duplicate message id"
            )
        if summary.id in covers:
            raise MessageRefError(
                f"compaction at seq {event.seq} covers a non-contiguous range: "
                "the summary message id is also covered"
            )
        if summary.id in self.by_id:
            raise MessageError(
                f"event {event.type!r} at seq {event.seq} message is invalid: "
                f"duplicate message id {summary.id!r}"
            )
        positions: list[int] = []
        for message_id in covers:
            if message_id in self.covered:
                raise MessageRefError(
                    f"compaction at seq {event.seq} covers a non-contiguous range: "
                    f"{message_id!r} was already covered"
                )
            index = self.by_id.get(message_id)
            if index is None:
                raise MessageRefError(
                    f"event {event.type!r} at seq {event.seq} references unknown message id {message_id!r}"
                )
            positions.append(index)
        if positions != list(range(min(positions), max(positions) + 1)):
            raise MessageRefError(
                f"compaction at seq {event.seq} covers a non-contiguous range: "
                "covered messages are not adjacent"
            )
        start = positions[0]
        remove = set(covers)
        for message_id in covers:
            del self.by_id[message_id]
        self.covered.update(covers)
        remaining = [message for message in self.messages if message.id not in remove]
        remaining.insert(start, summary)
        self.messages = remaining
        self.by_id = {message.id: index for index, message in enumerate(self.messages)}

    def _approval_requested(self, event: Event) -> None:
        data = event.data
        approval_id = data["approval_id"]
        if approval_id in self.approvals_seen:
            raise OrderingError(
                f"event {event.type!r} at seq {event.seq} is not allowed here: "
                f"approval id {approval_id!r} is used more than once"
            )
        self.approvals_seen.add(approval_id)
        self.pending[approval_id] = ApprovalRecord(
            approval_id=approval_id,
            tool_call_id=data["tool_call_id"],
            tool_name=data["tool_name"],
            reason=data.get("reason"),
            requested_seq=event.seq,
            requested_ts=event.ts,
        )

    def _approval_decided(self, event: Event) -> None:
        approval_id = event.data["approval_id"]
        if approval_id not in self.pending:
            raise OrderingError(
                f"event {event.type!r} at seq {event.seq} is not allowed here: "
                f"approval id {approval_id!r} is not pending"
            )
        del self.pending[approval_id]

    def _task_state(self, event: Event) -> None:
        task = event.data["task"]
        self.tasks[task["id"]] = TaskFact(
            task_id=task["id"],
            objective=task["objective"],
            status=task["status"],
            owner=task.get("owner"),
            created_at=task["created_at"],
            seq=event.seq,
        )

    def _task_handoff(self, event: Event) -> None:
        data = event.data
        run_id = data["run_id"]
        if run_id in self.handoffs:
            raise OrderingError(
                f"event {event.type!r} at seq {event.seq} is not allowed here: "
                f"handoff run id {run_id!r} is used more than once"
            )
        self.handoffs[run_id] = HandoffFact(
            run_id=run_id,
            from_owner=data["from"],
            to_owner=data["to"],
            summary=data["summary"],
            task_id=data.get("task_id"),
            created_at=data["created_at"],
            seq=event.seq,
            open_items=tuple(data.get("open_items", ())),
        )

    def _task_handoff_result(self, event: Event) -> None:
        data = event.data
        run_id = data["run_id"]
        fact = self.handoffs.get(run_id)
        if fact is None:
            raise OrderingError(
                f"event {event.type!r} at seq {event.seq} is not allowed here: "
                f"handoff run id {run_id!r} has no task.handoff"
            )
        if fact.result_seq is not None:
            raise OrderingError(
                f"event {event.type!r} at seq {event.seq} is not allowed here: "
                f"handoff run id {run_id!r} already has a result"
            )
        self.handoffs[run_id] = replace(
            fact,
            result_ok=data["ok"],
            result=data["result"],
            error=data.get("error"),
            result_seq=event.seq,
        )

    def _register_calls(self, message: Message, event: Event) -> None:
        turn_id = _require_turn(event)
        step_id = _require_step(event)
        for part in message.tool_calls():
            if part.id in self.calls:
                raise ToolPairingError(f"call id {part.id!r} is used more than once")
            record = _CallRecord(
                call_id=part.id,
                turn_id=turn_id,
                step_id=step_id,
                assistant_seq=event.seq,
                assistant_message_id=message.id,
                part=part,
            )
            self.calls[part.id] = record
            self.turn_calls.setdefault(turn_id, []).append(part.id)

    def _tool_call(self, event: Event) -> None:
        data = event.data
        call_id = data["call_id"]
        record = self.calls.get(call_id)
        if record is None:
            raise ToolPairingError(
                f"tool.call {call_id!r} at seq {event.seq} has no matching assistant tool call"
            )
        if record.call_seq is not None:
            raise ToolPairingError(f"call id {call_id!r} is used more than once")
        if record.turn_id != event.turn_id or record.step_id != event.step_id:
            raise ToolPairingError(
                f"tool.call {call_id!r} at seq {event.seq} is not in the same turn and step "
                "as its assistant tool call"
            )
        if data["name"] != record.part.name or data["arguments"] != record.part.arguments:
            raise ToolPairingError(
                f"tool.call {call_id!r} at seq {event.seq} does not match the assistant tool call"
            )
        record.call_seq = event.seq
        record.call_ts = event.ts

    def _tool_result(self, event: Event) -> None:
        data = event.data
        call_id = data["call_id"]
        record = self.calls.get(call_id)
        if record is None or record.call_seq is None:
            raise ToolPairingError(f"tool.result at seq {event.seq} has no matching tool.call {call_id!r}")
        if record.result_seq is not None:
            raise ToolPairingError(f"call id {call_id!r} is used more than once")
        if record.turn_id != event.turn_id or record.step_id != event.step_id:
            raise ToolPairingError(
                f"tool.result at seq {event.seq} is not in the same turn and step as tool.call {call_id!r}"
            )
        record.result_seq = event.seq
        record.result_ts = event.ts
        self._append_message(self._message(event), event.seq)

    def _finish_turn(self, turn_id: str, reason: str, seq: int, ts: int) -> None:
        for call_id in self.turn_calls.get(turn_id, ()):
            record = self.calls[call_id]
            if record.call_seq is None:
                raise ToolPairingError(
                    f"assistant tool call {call_id!r} at seq {record.assistant_seq} has no matching tool.call"
                )
            if record.result_seq is not None:
                continue
            if reason == "completed":
                raise ToolPairingError(
                    f"tool.call {call_id!r} at seq {record.call_seq} has no matching tool.result"
                )
            repair = missing_tool_result_message(call_id, ts)
            self._append_message(repair, seq)
            self.issues.append(
                FoldIssue(
                    code="missing-tool-result",
                    message=(
                        f"tool.call {call_id!r} has no tool.result; derived a repair message "
                        f"because the turn ended as {reason!r}"
                    ),
                    seq=seq,
                    call_id=call_id,
                )
            )


def _require_turn(event: Event) -> str:
    if event.turn_id is None:
        raise OrderingError(f"event {event.type!r} at seq {event.seq} requires a turn_id")
    return event.turn_id


def _require_step(event: Event) -> str:
    if event.step_id is None:
        raise OrderingError(f"event {event.type!r} at seq {event.seq} requires a step_id")
    return event.step_id


def _goal_from_data(raw: dict[str, Any] | None) -> GoalSnapshot | None:
    if raw is None:
        return None
    blocked = raw.get("blocked_reason")
    return GoalSnapshot(
        id=raw["id"],
        revision=raw["revision"],
        objective=raw["objective"],
        phase=raw["phase"],
        max_goal_rounds=raw["max_goal_rounds"],
        blocked_reason=BlockedReason(blocked["code"], blocked["message"]) if blocked else None,
    )


def fold(header: SessionHeader, events: tuple[Event, ...] | list[Event]) -> SessionView:
    """Fold a validated schema 1 log into a deterministic session view."""
    return _Folder(header, tuple(events)).run()


__all__ = [
    "ApprovalRecord",
    "Deliverable",
    "FoldIssue",
    "HandoffFact",
    "ModelSelection",
    "PresetSelection",
    "SessionState",
    "SessionView",
    "TaskFact",
    "TodoItem",
    "fold",
]
