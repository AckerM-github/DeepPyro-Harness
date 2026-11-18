"""Subagent usage measurements: values, records baseline and supervisor aggregation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deeppyro.subagent import (
    SubagentUsage,
    Supervisor,
    SupervisorError,
    SupervisorLimits,
    SupervisorStatus,
    WorkerAssignment,
    WorkerBudget,
    WorkerOutcome,
    usage_from_records,
)


class UsageWorker:
    """Worker double that reports fixed usage and finishes in one round."""

    def __init__(self, usage: SubagentUsage, *, failed: bool = False, error: str = "") -> None:
        self.usage = usage
        self.failed = failed
        self.error = error
        self.assignments: list[WorkerAssignment] = []

    async def run(self, assignment: WorkerAssignment) -> WorkerOutcome:
        self.assignments.append(assignment)
        return WorkerOutcome(
            summary="" if self.failed else "child answer",
            steps=self.usage.steps,
            tokens=self.usage.input_tokens + self.usage.output_tokens,
            done=not self.failed,
            failed=self.failed,
            error=self.error or None,
            usage=self.usage,
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
    worker = UsageWorker(usage)
    outcome = await worker.run(WorkerAssignment("worker-1", "do the thing", 1, WorkerBudget()))

    assert outcome.usage == usage
    assert outcome.steps == 2
    assert outcome.tokens == 16
    assert outcome.to_dict()["usage"] == usage.to_dict()

    worker.assignments.clear()
    supervisor = Supervisor(worker, limits=SupervisorLimits(max_workers=2))
    result = await supervisor.run("do the thing")

    assert result.status is SupervisorStatus.CONVERGED
    assert len(worker.assignments) == 2
    assert result.usage.input_tokens == 24
    assert result.usage.output_tokens == 8
    assert result.usage.steps == 4
    assert result.to_dict()["usage"]["inputTokens"] == 24


async def test_supervisor_usage_survives_worker_failure() -> None:
    usage = SubagentUsage(input_tokens=5, output_tokens=1, steps=1)
    worker = UsageWorker(usage, failed=True, error="child exploded")

    result = await Supervisor(worker, limits=SupervisorLimits(max_workers=1)).run("do the thing")

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
