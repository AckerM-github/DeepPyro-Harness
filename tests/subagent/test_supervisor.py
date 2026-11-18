"""Tests for the budgeted supervisor and its worker protocol."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deeppyro.subagent import (
    MAX_WORKERS,
    Supervisor,
    SupervisorError,
    SupervisorLimits,
    SupervisorStatus,
    WorkerAssignment,
    WorkerBudget,
    WorkerOutcome,
)
from deeppyro.subagent.supervisor import WorkerRun


class PlanWorker:
    """Worker that returns a planned outcome per (worker id, round)."""

    def __init__(self, plan: dict[tuple[str, int], Any]) -> None:
        self.plan = plan
        self.assignments: list[WorkerAssignment] = []

    async def run(self, assignment: WorkerAssignment) -> WorkerOutcome:
        self.assignments.append(assignment)
        value = self.plan[(assignment.worker_id, assignment.round)]
        if isinstance(value, Exception):
            raise value
        return value


class SlowWorker:
    """Worker that sleeps for a fixed delay or until released."""

    def __init__(self, *, delay: float | None = None) -> None:
        self.delay = delay
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.assignments: list[WorkerAssignment] = []

    async def run(self, assignment: WorkerAssignment) -> WorkerOutcome:
        self.assignments.append(assignment)
        self.started.set()
        if self.delay is None:
            await self.release.wait()
        else:
            await asyncio.sleep(self.delay)
        return WorkerOutcome(summary="slow worker finished", done=True)


def test_supervisor_rejects_more_than_two_workers() -> None:
    worker = PlanWorker({})
    with pytest.raises(SupervisorError, match="at most 2 workers"):
        SupervisorLimits(max_workers=MAX_WORKERS + 1)
    with pytest.raises(SupervisorError, match="at most"):
        Supervisor(worker, budgets=[WorkerBudget() for _ in range(MAX_WORKERS + 1)])
    with pytest.raises(SupervisorError, match="at least one worker budget"):
        Supervisor(worker, budgets=[])
    with pytest.raises(SupervisorError, match="worker must implement the Worker protocol"):
        Supervisor(object())  # type: ignore[arg-type]
    with pytest.raises(SupervisorError, match="sequence of WorkerBudget"):
        Supervisor(worker, budgets="nope")  # type: ignore[arg-type]
    with pytest.raises(SupervisorError, match="must be WorkerBudget values"):
        Supervisor(worker, budgets=[object()])  # type: ignore[list-item]
    with pytest.raises(SupervisorError, match="supervisor limits"):
        Supervisor(worker, limits=object())  # type: ignore[arg-type]
    with pytest.raises(SupervisorError, match="clock must be callable"):
        Supervisor(worker, clock=object())  # type: ignore[arg-type]


def test_budget_and_outcome_validation() -> None:
    with pytest.raises(SupervisorError, match="positive integer"):
        WorkerBudget(max_steps=0)
    with pytest.raises(SupervisorError, match="positive number"):
        WorkerBudget(max_seconds=0)
    with pytest.raises(SupervisorError, match="unknown field"):
        WorkerBudget.from_dict({"max_steps": 2, "nope": 1})
    budget = WorkerBudget(max_steps=3, max_tokens=10, max_seconds=1.5)
    assert WorkerBudget.from_dict(budget.to_dict()) == budget
    with pytest.raises(SupervisorError, match="both done and failed"):
        WorkerOutcome(summary="x", done=True, failed=True)
    with pytest.raises(SupervisorError, match="non-negative"):
        WorkerOutcome(summary="x", steps=-1)
    with pytest.raises(SupervisorError, match="non-empty string"):
        WorkerAssignment(worker_id="", objective="x", round=1, budget=WorkerBudget())
    with pytest.raises(SupervisorError, match="positive integer"):
        WorkerAssignment(worker_id="worker-1", objective="x", round=0, budget=WorkerBudget())


async def test_converges_when_workers_agree() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): WorkerOutcome(summary="draft A"),
            ("worker-2", 1): WorkerOutcome(summary="draft B"),
            ("worker-1", 2): WorkerOutcome(summary="final answer"),
            ("worker-2", 2): WorkerOutcome(summary="Final   Answer"),
        }
    )
    supervisor = Supervisor(worker, limits=SupervisorLimits(max_rounds=5))
    result = await supervisor.run("summarize the task")
    assert result.status is SupervisorStatus.CONVERGED
    assert result.converged is True
    assert result.rounds == 2
    assert "workers agree on the same summary" in result.reason
    assert result.latest_summaries() == (
        ("worker-1", "final answer"),
        ("worker-2", "Final   Answer"),
    )
    assert len(result.runs) == 4
    assert [assignment.round for assignment in worker.assignments] == [1, 1, 2, 2]
    assert {assignment.objective for assignment in worker.assignments} == {"summarize the task"}


async def test_converges_when_every_worker_is_done() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): WorkerOutcome(summary="one", done=True),
            ("worker-2", 1): WorkerOutcome(summary="two", done=True),
        }
    )
    result = await Supervisor(worker).run("finish it")
    assert result.status is SupervisorStatus.CONVERGED
    assert result.rounds == 1
    assert result.reason == "every worker reported done after 1 round(s)"


async def test_converges_with_a_failed_worker() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): WorkerOutcome(summary="finished", done=True),
            ("worker-2", 1): RuntimeError("boom"),
        }
    )
    result = await Supervisor(worker).run("finish it")
    assert result.status is SupervisorStatus.CONVERGED
    assert "converged with 1 failed worker(s)" in result.reason
    failed_run = next(run for run in result.runs if run.worker_id == "worker-2")
    assert failed_run.ok is False
    assert failed_run.error == "RuntimeError: boom"


async def test_fails_when_no_worker_produces_an_outcome() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): RuntimeError("boom"),
            ("worker-2", 1): ValueError("bad"),
        }
    )
    result = await Supervisor(worker).run("finish it")
    assert result.status is SupervisorStatus.FAILED
    assert result.failed is True
    assert result.reason == "no worker produced a usable outcome in round 1"
    assert {run.error for run in result.runs} == {"RuntimeError: boom", "ValueError: bad"}


async def test_worker_reported_failure_is_recorded() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): WorkerOutcome(summary="", failed=True, error="policy denied"),
            ("worker-2", 1): WorkerOutcome(summary="ok", done=True),
        }
    )
    result = await Supervisor(worker).run("finish it")
    assert result.status is SupervisorStatus.CONVERGED
    failed_run = next(run for run in result.runs if run.worker_id == "worker-1")
    assert failed_run.error == "policy denied"
    assert result.latest_summaries() == (("worker-2", "ok"),)


async def test_invalid_worker_outcome_is_rejected() -> None:
    worker = PlanWorker({("worker-1", 1): "not an outcome"})
    supervisor = Supervisor(worker, limits=SupervisorLimits(max_workers=1))
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.FAILED
    assert result.runs[0].error == "worker returned an invalid outcome"


async def test_budget_exhaustion_rejects_further_rounds() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): WorkerOutcome(summary="alpha", steps=1),
            ("worker-2", 1): WorkerOutcome(summary="beta", steps=1),
        }
    )
    budgets = [WorkerBudget(max_steps=1), WorkerBudget(max_steps=1)]
    supervisor = Supervisor(worker, budgets=budgets, limits=SupervisorLimits(max_rounds=3))
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.FAILED
    assert result.rounds == 2
    assert len(worker.assignments) == 2
    rejected = [run for run in result.runs if run.rejected]
    assert len(rejected) == 2
    assert all(run.error == "worker budget exhausted" for run in rejected)


async def test_step_budget_overrun_is_recorded() -> None:
    worker = PlanWorker({("worker-1", 1): WorkerOutcome(summary="x", steps=3)})
    supervisor = Supervisor(worker, budgets=[WorkerBudget(max_steps=2)])
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.FAILED
    assert result.runs[0].error == "worker exceeded its step budget (3 > 2)"
    assert result.runs[0].ok is False


async def test_token_budget_overrun_is_recorded() -> None:
    worker = PlanWorker({("worker-1", 1): WorkerOutcome(summary="x", steps=1, tokens=11)})
    supervisor = Supervisor(worker, budgets=[WorkerBudget(max_tokens=10)])
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.FAILED
    assert result.runs[0].error == "worker exceeded its token budget (11 > 10)"


async def test_wall_clock_timeout_is_recorded() -> None:
    worker = SlowWorker(delay=0.1)
    supervisor = Supervisor(worker, budgets=[WorkerBudget(max_seconds=0.01)])
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.FAILED
    assert result.runs[0].error is not None
    assert "time budget" in result.runs[0].error
    assert result.runs[0].outcome is None


async def test_measured_time_overrun_is_recorded() -> None:
    now = [0.0]

    class ClockWorker:
        async def run(self, assignment: WorkerAssignment) -> WorkerOutcome:
            now[0] += 5.0
            return WorkerOutcome(summary="slow", done=True)

    supervisor = Supervisor(ClockWorker(), budgets=[WorkerBudget(max_seconds=1)], clock=lambda: now[0])
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.FAILED
    assert result.runs[0].error is not None
    assert "time budget" in result.runs[0].error
    assert "5.000s" in result.runs[0].error


async def test_stall_is_detected() -> None:
    plan: dict[tuple[str, int], Any] = {}
    for round_index in range(1, 6):
        plan[("worker-1", round_index)] = WorkerOutcome(summary="same alpha")
        plan[("worker-2", round_index)] = WorkerOutcome(summary="same beta")
    supervisor = Supervisor(
        PlanWorker(plan),
        limits=SupervisorLimits(max_rounds=5, stall_rounds=2),
    )
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.STALLED
    assert result.rounds == 3
    assert result.reason == "worker summaries unchanged for 3 rounds"


async def test_round_limit_is_reported() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): WorkerOutcome(summary="a1"),
            ("worker-2", 1): WorkerOutcome(summary="b1"),
            ("worker-1", 2): WorkerOutcome(summary="a2"),
            ("worker-2", 2): WorkerOutcome(summary="b2"),
        }
    )
    supervisor = Supervisor(worker, limits=SupervisorLimits(max_rounds=2))
    result = await supervisor.run("finish it")
    assert result.status is SupervisorStatus.ROUND_LIMIT
    assert result.rounds == 2
    assert result.reason == "no convergence after 2 rounds"


async def test_cancel_stops_the_run() -> None:
    worker = SlowWorker()
    supervisor = Supervisor(worker, budgets=[WorkerBudget()])
    task = asyncio.create_task(supervisor.run("finish it"))
    await asyncio.wait_for(worker.started.wait(), timeout=2)
    assert supervisor.running is True
    supervisor.cancel()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.status is SupervisorStatus.CANCELLED
    assert result.cancelled is True
    assert supervisor.cancelled is True
    assert supervisor.running is False


async def test_concurrent_runs_are_rejected() -> None:
    worker = SlowWorker()
    supervisor = Supervisor(worker, budgets=[WorkerBudget()])
    task = asyncio.create_task(supervisor.run("finish it"))
    await asyncio.wait_for(worker.started.wait(), timeout=2)
    with pytest.raises(SupervisorError, match="already running"):
        await supervisor.run("again")
    worker.release.set()
    result = await asyncio.wait_for(task, timeout=2)
    assert result.status is SupervisorStatus.CONVERGED
    with pytest.raises(SupervisorError, match="non-empty string"):
        await supervisor.run("   ")


async def test_result_aggregation_is_deterministic() -> None:
    worker = PlanWorker(
        {
            ("worker-1", 1): WorkerOutcome(summary="one", done=True),
            ("worker-2", 1): WorkerOutcome(summary="two", done=True),
        }
    )
    result = await Supervisor(worker).run("finish it")
    data = result.to_dict()
    assert data["status"] == "converged"
    assert data["rounds"] == 1
    assert data["summaries"] == [
        {"worker_id": "worker-1", "summary": "one"},
        {"worker_id": "worker-2", "summary": "two"},
    ]
    assert result.summary_text() == "worker-1: one\nworker-2: two"
    assert all(isinstance(run_data, dict) for run_data in data["runs"])  # type: ignore[union-attr]


def test_worker_run_fields() -> None:
    run = WorkerRun(
        worker_id="worker-1",
        round=2,
        outcome=WorkerOutcome(summary="hello"),
        error=None,
        rejected=False,
    )
    assert run.ok is True
    assert run.summary == "hello"
    data = run.to_dict()
    assert data["ok"] is True
    assert data["round"] == 2
    assert data["outcome"] == WorkerOutcome(summary="hello").to_dict()
