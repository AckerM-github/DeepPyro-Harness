"""KI-011: task_handoff records native task facts end to end.

These tests drive TaskHandoffTool against a real NativeSession (no v4 adapter,
no core.wire): the tool must write task.state / task.handoff /
task.handoff.result events and the same facts must fold back after a reopen.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from deeppyro.agent.task import TaskBudget, TaskStatus
from deeppyro.builtin.task_tools import (
    TASK_TOOL_BUDGET_EXCEEDED,
    TASK_TOOL_HANDOFF_FAILED,
    TaskHandoffTool,
    TaskService,
    TaskToolError,
)
from deeppyro.core.protocols import ToolContext
from deeppyro.session.fold import fold
from deeppyro.session.schema import NativeSession
from deeppyro.subagent import SubagentResult, SubagentUsage

SESSION_ID = "session-00000000-0000-4000-8000-0000000000b1"
LOG_USAGE_KEYS = frozenset(
    {
        "usage",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "cost_usd",
    }
)


class FakeSubagent:
    """The minimal subagent service surface task_handoff consumes."""

    def __init__(
        self,
        *,
        text: str = "child result",
        is_error: bool = False,
        error: str = "",
        usage: SubagentUsage | None = None,
        raises: Exception | None = None,
        cancel: bool = False,
    ) -> None:
        self.text = text
        self.is_error = is_error
        self.error = error
        self.usage = usage
        self.raises = raises
        self.cancel = cancel
        self.prompts: list[str] = []

    async def run(self, prompt: str, *, description: str, **kwargs: Any) -> SubagentResult:
        self.prompts.append(prompt)
        if self.cancel:
            raise asyncio.CancelledError
        if self.raises is not None:
            raise self.raises
        return SubagentResult(
            child_id="child-1",
            session_id="session-child",
            label="handoff",
            provider="fake",
            model="fake-model",
            text=self.text,
            is_error=self.is_error,
            error=self.error,
            usage=self.usage,
        )


def _session(tmp_path: Path, session_id: str = SESSION_ID) -> tuple[NativeSession, Path]:
    path = tmp_path / "native" / f"{session_id}.jsonl"
    session = NativeSession.create(
        path,
        cwd=tmp_path,
        session_id=session_id,
        created_at=1_700_000_000_000,
    )
    return session, path


def _context(session: NativeSession, subagent: FakeSubagent) -> ToolContext:
    return ToolContext(
        cwd=Path("C:/work"),
        session_id=session.header.id,
        turn=1,
        step=1,
        call_id="call-1",
        cancel=asyncio.Event(),
        log=lambda *_: None,
        services={"session": session, "subagent": subagent},
    )


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, Mapping):
        keys = set(value)
        for item in value.values():
            keys |= _walk_keys(item)
        return keys
    if isinstance(value, (list, tuple)):
        keys: set[str] = set()
        for item in value:
            keys |= _walk_keys(item)
        return keys
    return set()


async def test_task_handoff_writes_native_task_facts(tmp_path: Path) -> None:
    session, path = _session(tmp_path)
    service = TaskService()
    subagent = FakeSubagent(
        text="handoff answer",
        usage=SubagentUsage(input_tokens=40, output_tokens=11, steps=1),
    )
    tool = TaskHandoffTool(service)
    payload: dict[str, Any] = {}
    try:
        result = await tool.run(
            {
                "prompt": "write the report",
                "summary": "carry the context",
                "open_items": ["append appendix"],
            },
            _context(session, subagent),
        )
        payload = json.loads(result.text)
        run_id = payload["runId"]
        task_id = payload["task"]["id"]

        assert payload["task"]["status"] == "completed"
        assert payload["result"] == "handoff answer"
        assert payload["openItems"] == ["append appendix"]
        assert payload["usage"]["inputTokens"] == 40
        assert run_id.startswith("handoff-")
        assert payload["handoff"]["from"] == SESSION_ID
        assert payload["handoff"]["to"] == f"subagent:{run_id}"
        assert payload["handoff"]["state"]["turn"] == 1
        assert payload["handoff"]["state"]["callId"] == "call-1"
        assert payload["handoff"]["state"]["budget"] == {"max_steps": 32}
        assert payload["handoff"]["state"]["prompt"] == "write the report"
        # Native schema 1 keeps token facts out of the session log (schema 8.5):
        # the parent snapshot handed to the child is zeros on a native session.
        assert payload["handoff"]["state"]["usage"] == {"input": 0, "output": 0, "cached": 0}

        assert [event.type for event in session.events] == [
            "task.state",
            "task.state",
            "task.handoff",
            "task.handoff.result",
            "task.state",
        ]
        assert all(event.run_id is None for event in session.events)
        handoff_event = session.events[2]
        assert handoff_event.data["run_id"] == run_id
        assert handoff_event.data["task_id"] == task_id
        assert handoff_event.data["from"] == SESSION_ID
        assert handoff_event.data["open_items"] == ["append appendix"]
        assert handoff_event.data["prompt"] == "write the report"
        result_event = session.events[3]
        assert result_event.data == {
            "run_id": run_id,
            "task_id": task_id,
            "ok": True,
            "result": "handoff answer",
        }
        for event in session.events:
            if event.type in ("task.handoff", "task.handoff.result"):
                assert not (LOG_USAGE_KEYS & _walk_keys(event.data))

        view = fold(session.header, session.events)
        assert view.issues == ()
        assert [fact.status for fact in view.tasks] == ["completed"]
        assert view.handoffs[0].run_id == run_id
        assert view.handoffs[0].task_id == task_id
        assert view.handoffs[0].result_ok is True
        assert view.handoffs[0].result == "handoff answer"

        packet = subagent.prompts[0]
        assert packet.startswith("write the report")
        assert "[handoff context]" in packet

        assert service.get(session, task_id).status is TaskStatus.COMPLETED
        assert service.pending_handoffs(session) == ()
        assert service.handoff_result(session, run_id)["ok"] is True
    finally:
        session.close()

    reopened = NativeSession.open(path)
    try:
        resumed = TaskService()
        assert resumed.get(reopened, payload["task"]["id"]).status is TaskStatus.COMPLETED
        result_fact = resumed.handoff_result(reopened, payload["runId"])
        assert result_fact is not None
        assert result_fact["ok"] is True
        assert result_fact["result"] == "handoff answer"
        assert result_fact["usage"] is None
        run = resumed.handoffs(reopened)[0]
        assert run["runId"] == payload["runId"]
        assert run["handoff"]["task_id"] == payload["task"]["id"]
    finally:
        reopened.close()


async def test_native_handoff_failure_is_a_native_fact(tmp_path: Path) -> None:
    session, _ = _session(tmp_path, "session-00000000-0000-4000-8000-0000000000b2")
    service = TaskService()
    subagent = FakeSubagent(text="", is_error=True, error="upstream 500")
    try:
        with pytest.raises(TaskToolError) as excinfo:
            await TaskHandoffTool(service).run(
                {"prompt": "work"},
                _context(session, subagent),
            )
        assert excinfo.value.code == TASK_TOOL_HANDOFF_FAILED
        assert "upstream 500" in str(excinfo.value)

        assert [event.type for event in session.events] == [
            "task.state",
            "task.state",
            "task.handoff",
            "task.handoff.result",
            "task.state",
        ]
        result_event = session.events[3]
        assert result_event.data["ok"] is False
        assert result_event.data["result"] == ""
        assert result_event.data["error"] == "upstream 500"
        view = fold(session.header, session.events)
        assert view.issues == ()
        assert view.handoffs[0].result_ok is False
        assert view.handoffs[0].error == "upstream 500"
        assert [fact.status for fact in view.tasks] == ["failed"]
        assert service.handoff_result(session, view.handoffs[0].run_id)["usage"] is None
    finally:
        session.close()


async def test_native_handoff_budget_exceeded_fails_existing_task(tmp_path: Path) -> None:
    session, _ = _session(tmp_path, "session-00000000-0000-4000-8000-0000000000b3")
    service = TaskService()
    task = service.create(session, "bounded work", budget=TaskBudget(max_tokens=5))
    subagent = FakeSubagent(
        text="too much work",
        usage=SubagentUsage(input_tokens=10, output_tokens=5, steps=1, latency_ms=5),
    )
    try:
        with pytest.raises(TaskToolError) as excinfo:
            await TaskHandoffTool(service).run(
                {"prompt": "work", "task_id": task.id},
                _context(session, subagent),
            )
        assert excinfo.value.code == TASK_TOOL_BUDGET_EXCEEDED
        assert "15 tokens > max_tokens 5" in str(excinfo.value)

        assert [event.type for event in session.events] == [
            "task.state",
            "task.state",
            "task.handoff",
            "task.handoff.result",
            "task.state",
        ]
        result_event = session.events[3]
        assert result_event.data["ok"] is True
        assert "usage" not in result_event.data
        view = fold(session.header, session.events)
        assert view.issues == ()
        assert [(fact.task_id, fact.status) for fact in view.tasks] == [(task.id, "failed")]
        assert service.get(session, task.id).status is TaskStatus.FAILED
    finally:
        session.close()


async def test_native_handoff_cancellation_is_a_native_fact(tmp_path: Path) -> None:
    session, _ = _session(tmp_path, "session-00000000-0000-4000-8000-0000000000b4")
    service = TaskService()
    subagent = FakeSubagent(cancel=True)
    try:
        with pytest.raises(asyncio.CancelledError):
            await TaskHandoffTool(service).run({"prompt": "work"}, _context(session, subagent))
        assert [event.type for event in session.events] == [
            "task.state",
            "task.state",
            "task.handoff",
            "task.handoff.result",
            "task.state",
        ]
        result_event = session.events[3]
        assert result_event.data["ok"] is False
        assert result_event.data["error"] == "cancelled"
        assert service.tasks(session)[0].status is TaskStatus.FAILED
    finally:
        session.close()
