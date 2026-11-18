"""KI-017: task_handoff persists a handoff fact, runs a child and folds the result back."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent.task import TaskBudget, TaskStatus, new_handoff
from deeppyro.builtin.task_tools import (
    HANDOFF_EVENT,
    HANDOFF_RESULT_EVENT,
    TASK_TOOL_BUDGET_EXCEEDED,
    TASK_TOOL_HANDOFF_FAILED,
    TASK_TOOL_INVALID_TRANSITION,
    TASK_TOOL_SUBAGENT_REQUIRED,
    TaskHandoffTool,
    TaskService,
    TaskToolError,
)
from deeppyro.core.protocols import ToolContext
from deeppyro.session.session import Session
from deeppyro.session.store import SessionStore
from deeppyro.subagent import SubagentResult, SubagentUsage
from tests.session import support
from tests.subagent.helpers import FakeAgent, FakeAgents, make_subagent, records


class HandoffAgent(FakeAgent):
    """Child agent double that records usage in its real child session."""

    def __init__(
        self,
        session: Session,
        *,
        reply: str = "handoff answer",
        input_tokens: int = 40,
        output_tokens: int = 11,
        cached_tokens: int = 5,
        retries: int = 1,
    ) -> None:
        super().__init__(reply)
        self.session = session
        self.reply = reply
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.retries = retries

    async def send(self, text: str) -> str:
        self.prompts.append(text)
        self.session.append("turn/start", {"turn": 1})
        self.session.append("step/start", {"turn": 1, "step": 1})
        payload = support.assistant_message("msg_child", self.reply)
        payload["usage"] = {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cached_tokens,
        }
        self.session.append("assistant/message", payload)
        for index in range(self.retries):
            self.session.append("llm/retry", {"retry": index + 1, "retryId": f"retry-{index}"})
        self.session.append("step/end", {"turn": 1, "step": 1})
        self.session.append("turn/end", {"turn": 1})
        return self.reply


class HandoffAgents(FakeAgents):
    """Agents double that builds one HandoffAgent per create()."""

    def create(self, **kwargs: Any) -> HandoffAgent:
        agent = HandoffAgent(kwargs["session"])
        self.created.append(kwargs)
        self.agents.append(agent)
        return agent


class FakeSubagent:
    """Subagent service double that returns a scripted SubagentResult."""

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
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt: str, *, description: str, **kwargs: Any) -> SubagentResult:
        self.calls.append((prompt, description))
        if self.cancel:
            raise asyncio.CancelledError
        if self.raises is not None:
            raise self.raises
        return SubagentResult(
            child_id="child-1",
            session_id="session-1",
            label="label",
            provider="fake",
            model="fake-model",
            text=self.text,
            is_error=self.is_error,
            error=self.error,
            usage=self.usage,
        )


def _seed_parent_turn(session: Session) -> None:
    session.append("turn/start", {"turn": 1})
    session.append("step/start", {"turn": 1, "step": 1})
    session.append("user/message", support.user_message("msg_user", "please hand this off"))
    session.append("assistant/message", support.assistant_message("msg_assistant", "handing off"))
    session.append("step/end", {"turn": 1, "step": 1})
    session.append("turn/end", {"turn": 1})


def _session(tmp_path: Path) -> tuple[SessionStore, Session]:
    store = SessionStore(root=tmp_path / "sessions")
    return store, store.create(tmp_path)


def _context(session: Session, *, services: dict[str, Any], turn: int = 1) -> ToolContext:
    return ToolContext(
        cwd=Path("C:/work"),
        session_id=session.session_id,
        turn=turn,
        step=1,
        call_id="call-1",
        cancel=asyncio.Event(),
        log=lambda *_: None,
        services=services,
        agent=SimpleNamespace(session=session, agent_id="lead", turn=turn),
    )


async def test_handoff_end_to_end_with_mock_provider(tmp_path: Path) -> None:
    agents = HandoffAgents()
    env = make_subagent(tmp_path, agents=agents)
    _seed_parent_turn(env.parent)
    service = TaskService()
    tool = TaskHandoffTool(service)

    result = await tool.run(
        {
            "prompt": "write the report",
            "summary": "carry the context",
            "open_items": ["append appendix"],
        },
        _context(env.parent, services={"subagent": env.service}),
    )

    payload = json.loads(result.text)
    assert result.meta == payload
    assert payload["task"]["status"] == "completed"
    assert payload["result"] == "handoff answer"
    task_id = payload["task"]["id"]
    run_id = payload["runId"]
    assert task_id.startswith("task_")
    assert run_id.startswith("handoff-")
    assert payload["openItems"] == ["append appendix"]
    usage = payload["usage"]
    assert usage is not None
    assert (usage["inputTokens"], usage["outputTokens"], usage["cachedTokens"]) == (40, 11, 5)
    assert (usage["retries"], usage["steps"]) == (1, 1)
    assert (usage["model"], usage["provider"]) == ("parent-model", "local")
    assert usage["latencyMs"] >= 0

    handoff_facts = records(env.parent, HANDOFF_EVENT)
    assert len(handoff_facts) == 1
    handoff = handoff_facts[0]["handoff"]
    assert handoff["from"] == "lead"
    assert handoff["to"] == f"subagent:{run_id}"
    assert handoff["summary"] == "carry the context"
    assert handoff["open_items"] == ["append appendix"]
    assert handoff["task_id"] == task_id
    assert handoff["state"]["turn"] == 1
    assert handoff["state"]["callId"] == "call-1"
    assert handoff["state"]["prompt"] == "write the report"
    assert handoff["state"]["usage"] == {"input": 10, "output": 5, "cached": 2}
    assert handoff["state"]["budget"] == {"max_steps": 32}

    result_facts = records(env.parent, HANDOFF_RESULT_EVENT)
    assert len(result_facts) == 1
    assert result_facts[0]["ok"] is True
    assert result_facts[0]["result"] == "handoff answer"
    assert result_facts[0]["usage"]["inputTokens"] == 40

    assert service.get(env.parent, task_id).status is TaskStatus.COMPLETED
    assert env.parent.usage() == {"input": 10, "output": 5, "cached": 2}
    assert env.service.usage() is not None
    assert env.service.usage().input_tokens == 40  # type: ignore[union-attr]
    sent = agents.agents[0].prompts[0]
    assert sent.startswith("write the report")
    assert "[handoff context]" in sent
    assert '"summary":"carry the context"' in sent
    assert '"max_steps":32' in sent
    assert '"parentUsage":{"cached":2,"input":10,"output":5}' in sent

    reopened = env.store.open(env.parent.session_id)
    resumed = TaskService()
    assert resumed.get(reopened, task_id).status is TaskStatus.COMPLETED
    assert resumed.pending_handoffs(reopened) == ()
    resumed_usage = resumed.handoff_result(reopened, run_id)["usage"]
    assert resumed_usage["inputTokens"] == 40


async def test_handoff_summarizes_the_current_turn_by_default(tmp_path: Path) -> None:
    _store, session = _session(tmp_path)
    _seed_parent_turn(session)
    service = TaskService()
    tool = TaskHandoffTool(service)
    subagent = FakeSubagent(usage=SubagentUsage(input_tokens=1, output_tokens=1))

    await tool.run({"prompt": "delegate this"}, _context(session, services={"subagent": subagent}))

    summary = records(session, HANDOFF_EVENT)[0]["handoff"]["summary"]
    assert "handing off" in summary
    assert "delegate this" not in summary


async def test_handoff_budget_exceeded_fails_the_task(tmp_path: Path) -> None:
    _store, session = _session(tmp_path)
    service = TaskService()
    task = service.create(session, "bounded work", budget=TaskBudget(max_tokens=5))
    tool = TaskHandoffTool(service)
    subagent = FakeSubagent(
        text="too much work",
        usage=SubagentUsage(input_tokens=10, output_tokens=5, steps=1, latency_ms=5),
    )

    with pytest.raises(TaskToolError) as excinfo:
        await tool.run(
            {"prompt": "work", "task_id": task.id},
            _context(session, services={"subagent": subagent}),
        )

    assert excinfo.value.code == TASK_TOOL_BUDGET_EXCEEDED
    assert "exceeded its budget" in str(excinfo.value)
    assert "15 tokens > max_tokens 5" in str(excinfo.value)
    assert service.get(session, task.id).status is TaskStatus.FAILED
    result_fact = records(session, HANDOFF_RESULT_EVENT)[0]
    assert result_fact["ok"] is True
    assert result_fact["usage"]["inputTokens"] == 10


async def test_handoff_failed_subagent_keeps_partial_usage(tmp_path: Path) -> None:
    _store, session = _session(tmp_path)
    service = TaskService()
    tool = TaskHandoffTool(service)
    usage = SubagentUsage(input_tokens=7, output_tokens=2, cached_tokens=1, retries=2, steps=2)
    subagent = FakeSubagent(is_error=True, error="upstream 500", usage=usage)

    with pytest.raises(TaskToolError) as excinfo:
        await tool.run({"prompt": "work"}, _context(session, services={"subagent": subagent}))

    assert excinfo.value.code == TASK_TOOL_HANDOFF_FAILED
    assert "upstream 500" in str(excinfo.value)
    task = service.tasks(session)[0]
    assert task.status is TaskStatus.FAILED
    facts = records(session, HANDOFF_RESULT_EVENT)
    assert facts[0]["ok"] is False
    assert facts[0]["error"] == "upstream 500"
    assert facts[0]["usage"]["retries"] == 2
    assert facts[0]["usage"]["steps"] == 2


async def test_cancelled_handoff_records_partial_usage_and_fails_task(tmp_path: Path) -> None:
    _store, session = _session(tmp_path)
    service = TaskService()
    tool = TaskHandoffTool(service)
    subagent = FakeSubagent(cancel=True)

    with pytest.raises(asyncio.CancelledError):
        await tool.run({"prompt": "work"}, _context(session, services={"subagent": subagent}))

    assert service.tasks(session)[0].status is TaskStatus.FAILED
    assert records(session, HANDOFF_RESULT_EVENT)[0]["error"] == "cancelled"


async def test_handoff_requires_the_subagent_service(tmp_path: Path) -> None:
    _store, session = _session(tmp_path)
    tool = TaskHandoffTool(TaskService())

    with pytest.raises(TaskToolError) as excinfo:
        await tool.run({"prompt": "work"}, _context(session, services={}))

    assert excinfo.value.code == TASK_TOOL_SUBAGENT_REQUIRED
    assert "subagent" in str(excinfo.value)


async def test_terminal_task_cannot_start_a_handoff(tmp_path: Path) -> None:
    _store, session = _session(tmp_path)
    service = TaskService()
    task = service.create(session, "finished already")
    service.update(session, task.id, TaskStatus.CANCELLED)
    tool = TaskHandoffTool(service)

    with pytest.raises(TaskToolError) as excinfo:
        await tool.run(
            {"prompt": "work", "task_id": task.id},
            _context(session, services={"subagent": FakeSubagent()}),
        )

    assert excinfo.value.code == TASK_TOOL_INVALID_TRANSITION
    assert "cannot start a handoff" in str(excinfo.value)


async def test_handoff_on_existing_task_advances_the_same_task(tmp_path: Path) -> None:
    _store, session = _session(tmp_path)
    service = TaskService()
    task = service.create(session, "delegated", owner="lead")
    tool = TaskHandoffTool(service)

    await tool.run(
        {"prompt": "finish it", "task_id": task.id},
        _context(session, services={"subagent": FakeSubagent(usage=SubagentUsage(steps=1))}),
    )

    assert len(service.tasks(session)) == 1
    completed = service.get(session, task.id)
    assert completed.status is TaskStatus.COMPLETED
    assert completed.owner == "lead"


async def test_interrupted_handoff_recovers_after_reopen(tmp_path: Path) -> None:
    store, session = _session(tmp_path)
    service = TaskService()
    task = service.create(session, "unfinished handoff")
    service.update(session, task.id, TaskStatus.IN_PROGRESS)
    handoff = new_handoff(
        "lead",
        "subagent:handoff-resume",
        "carry on",
        state={"turn": 1},
        open_items=("resume me",),
        task_id=task.id,
    )
    service.start_handoff(session, handoff, run_id="handoff-resume")

    reopened = store.open(session.session_id)
    resumed = TaskService()

    assert [fact["runId"] for fact in resumed.pending_handoffs(reopened)] == ["handoff-resume"]
    assert [candidate.id for candidate in resumed.unfinished(reopened)] == [task.id]
    assert resumed.handoff_result(reopened, "handoff-resume") is None

    resumed.finish_handoff(
        reopened,
        run_id="handoff-resume",
        task_id=task.id,
        ok=True,
        result="resumed result",
        usage=SubagentUsage(input_tokens=3, output_tokens=1, retries=1, steps=1),
    )
    resumed.update(reopened, task.id, TaskStatus.COMPLETED)

    assert resumed.pending_handoffs(reopened) == ()
    assert resumed.get(reopened, task.id).status is TaskStatus.COMPLETED
    stored = resumed.handoff_result(reopened, "handoff-resume")
    assert stored is not None
    assert stored["result"] == "resumed result"
    assert stored["usage"]["retries"] == 1
