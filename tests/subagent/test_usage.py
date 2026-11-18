"""KI-015: real provider usage flows from child sessions into results and runs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.session.session import Session
from deeppyro.subagent import (
    SubagentResult,
    SubagentService,
    SubagentServiceWorker,
    SubagentUsage,
    Supervisor,
    SupervisorError,
    SupervisorLimits,
    SupervisorStatus,
    WorkerAssignment,
    WorkerBudget,
    WorkerOutcome,
    sum_usages,
    usage_from_records,
)
from tests.session import support
from tests.subagent.helpers import FakeAgent, FakeAgents, make_subagent, records, wait_for


class UsageAgent(FakeAgent):
    """Agent double that writes native usage facts into its real child session."""

    def __init__(
        self,
        session: Session,
        *,
        reply: str = "ok",
        input_tokens: int = 12,
        output_tokens: int = 4,
        cached_tokens: int = 3,
        retries: int = 0,
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(reply, gate=gate, error=error)
        self.session = session
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens
        self.retries = retries
        self.turn = 0

    async def send(self, text: str) -> str:
        self.prompts.append(text)
        self.turn += 1
        turn = self.turn
        self.session.append("turn/start", {"turn": turn})
        self.session.append("step/start", {"turn": turn, "step": 1})
        self.session.append("user/message", support.user_message(f"msg_user_{turn}", text))
        payload = support.assistant_message(f"msg_assistant_{turn}", f"reply {turn}", turn=turn, step=1)
        payload["usage"] = {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "cacheReadTokens": self.cached_tokens,
        }
        self.session.append("assistant/message", payload)
        for index in range(self.retries):
            self.session.append(
                "llm/retry",
                {"retry": index + 1, "retryId": f"retry-{turn}-{index}", "turn": turn, "step": 1},
            )
        self.session.append("step/end", {"turn": turn, "step": 1})
        self.session.append("turn/end", {"turn": turn})
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.reply


class ScriptedAgents(FakeAgents):
    """Agents double that builds one UsageAgent per create() from a script."""

    def __init__(self, scripts: list[dict[str, Any]]) -> None:
        super().__init__()
        self.scripts = list(scripts)

    def create(self, **kwargs: Any) -> UsageAgent:
        script = self.scripts.pop(0)
        agent = UsageAgent(kwargs["session"], **script)
        self.created.append(kwargs)
        self.agents.append(agent)
        return agent


class RecordingSink:
    """Optional usage sink double (structural match for the KI-018 service)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int,
    ) -> None:
        self.calls.append(
            {
                "provider": provider,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens,
            }
        )


class FakeParentAgent:
    """Parent agent double that records settlement notices."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def followup(self, text: str, *, source: dict[str, Any] | None = None) -> None:
        self.calls.append((text, source))


async def test_single_subagent_reports_real_usage(tmp_path: Path) -> None:
    agents = ScriptedAgents([{"input_tokens": 12, "output_tokens": 4, "cached_tokens": 3, "retries": 2}])
    env = make_subagent(tmp_path, agents=agents)

    result = await env.service.run("go", description="single")

    assert result.is_error is False
    usage = result.usage
    assert isinstance(usage, SubagentUsage)
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (12, 4, 3)
    assert usage.retries == 2
    assert usage.steps == 1
    assert usage.model == "parent-model"
    assert usage.provider == "local"
    assert usage.latency_ms >= 0
    assert usage.total_tokens == 16
    assert env.service.usage(result.child_id) == usage
    total = env.service.usage()
    assert total is not None
    assert (total.input_tokens, total.output_tokens, total.cached_tokens) == (12, 4, 3)

    facts = records(env.parent, "subagent/usage")
    assert len(facts) == 1
    assert facts[0]["childId"] == result.child_id
    assert facts[0]["usage"]["inputTokens"] == 12
    assert facts[0]["usage"]["retries"] == 2


async def test_concurrent_subagents_aggregate_usage(tmp_path: Path) -> None:
    agents = ScriptedAgents(
        [
            {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 1},
            {"input_tokens": 20, "output_tokens": 4, "cached_tokens": 2},
            {"input_tokens": 30, "output_tokens": 6, "cached_tokens": 3},
        ]
    )
    env = make_subagent(tmp_path, agents=agents)

    results = await asyncio.gather(
        env.service.run("one", description="first"),
        env.service.run("two", description="second"),
        env.service.run("three", description="third"),
    )

    usages = [result.usage for result in results]
    assert all(isinstance(usage, SubagentUsage) for usage in usages)
    assert sorted((usage.input_tokens, usage.output_tokens, usage.cached_tokens) for usage in usages) == [
        (10, 2, 1),
        (20, 4, 2),
        (30, 6, 3),
    ]
    total = env.service.usage()
    assert total is not None
    assert (total.input_tokens, total.output_tokens, total.cached_tokens) == (60, 12, 6)
    assert total.steps == 3
    assert sum_usages(usages) == total
    assert len(records(env.parent, "subagent/usage")) == 3


async def test_failed_subagent_keeps_partial_usage(tmp_path: Path) -> None:
    agents = ScriptedAgents(
        [
            {
                "input_tokens": 7,
                "output_tokens": 3,
                "cached_tokens": 1,
                "retries": 1,
                "error": RuntimeError("provider exploded"),
            }
        ]
    )
    env = make_subagent(tmp_path, agents=agents)

    result = await env.service.run("go", description="fails")

    assert result.is_error is True
    assert "provider exploded" in result.error
    usage = result.usage
    assert isinstance(usage, SubagentUsage)
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (7, 3, 1)
    assert usage.retries == 1
    assert usage.steps == 1
    assert env.service.usage(result.child_id) == usage


async def test_cancelled_subagent_keeps_partial_usage(tmp_path: Path) -> None:
    gate = asyncio.Event()
    agents = ScriptedAgents([{"input_tokens": 9, "output_tokens": 5, "cached_tokens": 2, "gate": gate}])
    env = make_subagent(tmp_path, agents=agents)

    activation = await env.service.start("go", description="cancelled")

    async def child_has_facts() -> bool:
        child = env.store.open(activation.child_id)
        return any(record.type == "assistant/message" for record in child.records)

    await wait_for(lambda: len(records(env.parent, "subagent/catalog")) == 1)
    for _ in range(50):
        if await child_has_facts():
            break
        await asyncio.sleep(0)
    else:  # pragma: no cover - defensive
        raise AssertionError("child never wrote usage facts")

    assert await env.service.cancel_all() == 1

    await wait_for(lambda: env.service.usage(activation.child_id) is not None)
    usage = env.service.usage(activation.child_id)
    assert isinstance(usage, SubagentUsage)
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (9, 5, 2)
    assert usage.steps == 1


async def test_fork_usage_excludes_inherited_seed(tmp_path: Path) -> None:
    env = make_subagent(tmp_path, agents=ScriptedAgents([{}]))
    env.parent.append("turn/start", {"turn": 1})
    env.parent.append("step/start", {"turn": 1, "step": 1})
    env.parent.append("user/message", support.user_message())
    inherited = support.assistant_message("msg_parent", "parent answer")
    inherited["usage"] = {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2}
    env.parent.append("assistant/message", inherited)
    env.parent.append("step/end", {"turn": 1, "step": 1})
    env.parent.append("turn/end", {"turn": 1})
    env.service.bind_parent(FakeParentAgent())

    activation = await env.service.start("continue", description="forked", mode="fork")
    await env.service.wait_children(timeout=1)

    usage = env.service.usage(activation.child_id)
    assert isinstance(usage, SubagentUsage)
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (12, 4, 3)
    assert usage.steps == 1
    assert usage.model == "parent-model"


async def test_usage_sink_is_optional_and_decoupled(tmp_path: Path) -> None:
    sink = RecordingSink()
    agents = ScriptedAgents([{"input_tokens": 11, "output_tokens": 6, "cached_tokens": 2}])
    env = make_subagent(tmp_path, agents=agents, services={"usage": sink})

    await env.service.run("go", description="sink")

    assert sink.calls == [
        {
            "provider": "local",
            "model": "parent-model",
            "input_tokens": 11,
            "output_tokens": 6,
            "cached_tokens": 2,
        }
    ]


class FakeUsageService(SubagentService):
    """SubagentService double exposing provider usage in its results."""

    def __init__(
        self,
        usage: SubagentUsage,
        *,
        text: str = "child answer",
        is_error: bool = False,
        error: str = "",
    ) -> None:
        super().__init__()
        self.result_usage = usage
        self.text = text
        self.is_error = is_error
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def run(self, prompt: str, *, description: str, **kwargs: object) -> SubagentResult:
        self.calls.append((prompt, description))
        return SubagentResult(
            child_id="child-1",
            session_id="session-1",
            label="label",
            provider="fake",
            model="fake-model",
            text=self.text,
            is_error=self.is_error,
            error=self.error,
            usage=self.result_usage,
        )


async def test_supervisor_maps_and_aggregates_usage() -> None:
    usage = SubagentUsage(
        input_tokens=12,
        output_tokens=4,
        cached_tokens=3,
        latency_ms=5,
        model="fake-model",
        provider="fake",
        retries=1,
        steps=2,
    )
    service = FakeUsageService(usage)
    worker = SubagentServiceWorker(service)

    outcome = await worker.run(WorkerAssignment("worker-1", "do the thing", 1, WorkerBudget()))

    assert outcome.usage == usage
    assert outcome.steps == 2
    assert outcome.tokens == 16
    assert outcome.to_dict()["usage"] == usage.to_dict()

    service.calls.clear()
    supervisor = Supervisor(worker, limits=SupervisorLimits(max_workers=2))
    result = await supervisor.run("do the thing")

    assert result.status is SupervisorStatus.CONVERGED
    assert len(service.calls) == 2
    assert result.usage.input_tokens == 24
    assert result.usage.output_tokens == 8
    assert result.usage.steps == 4
    assert result.to_dict()["usage"]["inputTokens"] == 24


async def test_supervisor_usage_survives_worker_failure() -> None:
    usage = SubagentUsage(input_tokens=5, output_tokens=1, steps=1)
    service = FakeUsageService(usage, text="", is_error=True, error="child exploded")
    result = await Supervisor(SubagentServiceWorker(service), limits=SupervisorLimits(max_workers=1)).run(
        "do the thing"
    )

    assert result.status is SupervisorStatus.FAILED
    assert result.usage.input_tokens == 5
    outcome = result.runs[0].outcome
    assert outcome is not None
    assert outcome.usage == usage


def test_usage_values_validate_and_roundtrip() -> None:
    usage = SubagentUsage(input_tokens=1, output_tokens=2, model="m", provider="p")
    assert SubagentUsage.from_dict(usage.to_dict()) == usage
    assert (
        SubagentUsage.from_dict({"input_tokens": 1, "output_tokens": 2, "model": "m", "provider": "p"})
        == usage
    )
    with pytest.raises(Exception, match="unknown field"):
        SubagentUsage.from_dict({"inputTokens": 1, "mystery": 2})
    with pytest.raises(SupervisorError, match="usage"):
        WorkerOutcome(summary="x", usage=object())  # type: ignore[arg-type]


def test_usage_from_records_subtracts_baseline() -> None:
    records_list = [
        SimpleNamespace(
            type="assistant/message",
            data={"usage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 2}},
        ),
        SimpleNamespace(type="llm/retry", data={}),
        SimpleNamespace(
            type="assistant/message",
            data={"usage": {"inputTokens": 4, "outputTokens": 1, "cacheReadTokens": 1}},
        ),
    ]
    baseline = usage_from_records(records_list[:2])
    delta = usage_from_records(records_list, baseline=baseline, model="m", provider="p", latency_ms=9)

    assert (delta.input_tokens, delta.output_tokens, delta.cached_tokens) == (4, 1, 1)
    assert delta.retries == 0
    assert delta.steps == 1
    assert (delta.model, delta.provider, delta.latency_ms) == ("m", "p", 9)
