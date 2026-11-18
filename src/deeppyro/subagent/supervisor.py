"""Budgeted supervisor for a small pool of parallel workers.

Supervisor is the M4 multi-agent seam: it hands one objective to a pool of at
most two workers, enforces a per-worker budget across rounds, and aggregates the
latest worker summaries.  It detects convergence (every remaining worker is done
or all workers agree), stalls (unchanged outcomes repeat), failure (no worker
produced a usable outcome in a round) and cancellation.

The Worker protocol is intentionally tiny, so a real subagent adapter
(SubagentServiceWorker) and fake workers in tests are interchangeable.  The
supervisor enforces the wall-clock budget itself; step and token budgets are
checked against the counts a worker reports.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from deeppyro.subagent.errors import SubagentError
from deeppyro.subagent.manager import SubagentService
from deeppyro.subagent.types import SubagentResult, SubagentUsage, sum_usages

MAX_WORKERS = 2
DEFAULT_MAX_ROUNDS = 8
DEFAULT_STALL_ROUNDS = 2
DEFAULT_MAX_STEPS = 32


class SupervisorError(SubagentError):
    """A supervisor run was configured or used incorrectly."""


class SupervisorStatus(StrEnum):
    """Terminal status of one supervisor run."""

    CONVERGED = "converged"
    STALLED = "stalled"
    FAILED = "failed"
    ROUND_LIMIT = "round_limit"
    CANCELLED = "cancelled"


def _positive_int(value: object, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SupervisorError(f"{subject} must be a positive integer")
    return value


def _non_negative_int(value: object, subject: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise SupervisorError(f"{subject} must be a non-negative integer")
    return value


def _positive_number(value: object, subject: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise SupervisorError(f"{subject} must be a positive number")
    return float(value)


def _required_text(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupervisorError(f"{subject} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class WorkerBudget:
    """Cumulative budget for one worker across every supervisor round."""

    max_steps: int = DEFAULT_MAX_STEPS
    max_tokens: int | None = None
    max_seconds: float | None = None

    def __post_init__(self) -> None:
        _positive_int(self.max_steps, "worker budget max_steps")
        if self.max_tokens is not None:
            _positive_int(self.max_tokens, "worker budget max_tokens")
        if self.max_seconds is not None:
            _positive_number(self.max_seconds, "worker budget max_seconds")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
            "max_seconds": self.max_seconds,
        }

    @classmethod
    def from_dict(cls, raw: object) -> WorkerBudget:
        """Rebuild a budget from a JSON object, rejecting unknown fields."""
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise SupervisorError("worker budget must be a JSON object")
        allowed = {"max_steps", "max_tokens", "max_seconds"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise SupervisorError(f"worker budget has unknown field {unknown[0]!r}")
        max_steps = raw.get("max_steps", DEFAULT_MAX_STEPS)
        max_tokens = raw.get("max_tokens")
        max_seconds = raw.get("max_seconds")
        return cls(
            max_steps=_positive_int(max_steps, "worker budget max_steps"),
            max_tokens=None if max_tokens is None else _positive_int(max_tokens, "worker budget max_tokens"),
            max_seconds=None
            if max_seconds is None
            else _positive_number(max_seconds, "worker budget max_seconds"),
        )


@dataclass(frozen=True, slots=True)
class SupervisorLimits:
    """Static limits for one supervisor instance."""

    max_workers: int = MAX_WORKERS
    max_rounds: int = DEFAULT_MAX_ROUNDS
    stall_rounds: int = DEFAULT_STALL_ROUNDS

    def __post_init__(self) -> None:
        _positive_int(self.max_workers, "supervisor max_workers")
        if self.max_workers > MAX_WORKERS:
            raise SupervisorError(f"at most {MAX_WORKERS} workers are allowed")
        _positive_int(self.max_rounds, "supervisor max_rounds")
        _positive_int(self.stall_rounds, "supervisor stall_rounds")


@dataclass(frozen=True, slots=True)
class WorkerAssignment:
    """One round of work handed to a worker."""

    worker_id: str
    objective: str
    round: int
    budget: WorkerBudget

    def __post_init__(self) -> None:
        _required_text(self.worker_id, "worker assignment worker_id")
        _required_text(self.objective, "worker assignment objective")
        _positive_int(self.round, "worker assignment round")
        if not isinstance(self.budget, WorkerBudget):
            raise SupervisorError("worker assignment budget must be a WorkerBudget")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "worker_id": self.worker_id,
            "objective": self.objective,
            "round": self.round,
            "budget": self.budget.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    """What one worker reports after an assignment."""

    summary: str
    steps: int = 0
    tokens: int = 0
    done: bool = False
    failed: bool = False
    error: str | None = None
    usage: SubagentUsage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str):
            raise SupervisorError("worker outcome summary must be a string")
        _non_negative_int(self.steps, "worker outcome steps")
        _non_negative_int(self.tokens, "worker outcome tokens")
        if not isinstance(self.done, bool) or not isinstance(self.failed, bool):
            raise SupervisorError("worker outcome done and failed must be booleans")
        if self.done and self.failed:
            raise SupervisorError("worker outcome cannot be both done and failed")
        if self.error is not None and not isinstance(self.error, str):
            raise SupervisorError("worker outcome error must be a string or None")
        if self.usage is not None and not isinstance(self.usage, SubagentUsage):
            raise SupervisorError("worker outcome usage must be a SubagentUsage or None")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "summary": self.summary,
            "steps": self.steps,
            "tokens": self.tokens,
            "done": self.done,
            "failed": self.failed,
            "error": self.error,
            "usage": None if self.usage is None else self.usage.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class WorkerRun:
    """The record of one worker invocation (or a budget rejection)."""

    worker_id: str
    round: int
    outcome: WorkerOutcome | None = None
    error: str | None = None
    rejected: bool = False

    @property
    def ok(self) -> bool:
        """True when the worker returned a usable outcome."""
        return self.outcome is not None and self.error is None and not self.rejected

    @property
    def summary(self) -> str:
        """The worker summary, or an empty string when there is no outcome."""
        return "" if self.outcome is None else self.outcome.summary

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "worker_id": self.worker_id,
            "round": self.round,
            "outcome": None if self.outcome is None else self.outcome.to_dict(),
            "error": self.error,
            "rejected": self.rejected,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class SupervisorResult:
    """Aggregate outcome of one supervisor run."""

    status: SupervisorStatus
    rounds: int
    runs: tuple[WorkerRun, ...] = ()
    reason: str = ""

    @property
    def converged(self) -> bool:
        """True when the run converged."""
        return self.status is SupervisorStatus.CONVERGED

    @property
    def failed(self) -> bool:
        """True when no worker produced a usable outcome."""
        return self.status is SupervisorStatus.FAILED

    @property
    def cancelled(self) -> bool:
        """True when the run was cancelled."""
        return self.status is SupervisorStatus.CANCELLED

    @property
    def usage(self) -> SubagentUsage:
        """Provider usage reported by every worker run, aggregated.

        A run whose worker reports no usage contributes nothing, so a supervisor
        over plain workers yields zero counters.
        """
        return sum_usages(
            run.outcome.usage
            for run in self.runs
            if run.outcome is not None and run.outcome.usage is not None
        )

    def latest_summaries(self) -> tuple[tuple[str, str], ...]:
        """The latest summary per worker, sorted by worker id."""
        latest: dict[str, str] = {}
        for run in self.runs:
            if run.ok:
                latest[run.worker_id] = run.summary
        return tuple(sorted(latest.items()))

    def summary_text(self) -> str:
        """A deterministic, human-readable aggregation of worker summaries."""
        return "\n".join(f"{worker_id}: {summary}" for worker_id, summary in self.latest_summaries())

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return {
            "status": self.status.value,
            "rounds": self.rounds,
            "reason": self.reason,
            "runs": [run.to_dict() for run in self.runs],
            "usage": self.usage.to_dict(),
            "summaries": [
                {"worker_id": worker_id, "summary": summary} for worker_id, summary in self.latest_summaries()
            ],
        }


@runtime_checkable
class Worker(Protocol):
    """One budgeted worker execution backend."""

    async def run(self, assignment: WorkerAssignment) -> WorkerOutcome:
        """Execute one assignment and return its outcome."""
        ...


def _normalize_summary(summary: str) -> str:
    return " ".join(summary.casefold().split())


@dataclass(slots=True)
class _BudgetAccount:
    total: WorkerBudget
    steps: int = 0
    tokens: int = 0
    seconds: float = 0.0

    def remaining(self) -> WorkerBudget | None:
        steps_left = self.total.max_steps - self.steps
        if steps_left <= 0:
            return None
        tokens_left = None if self.total.max_tokens is None else self.total.max_tokens - self.tokens
        if tokens_left is not None and tokens_left <= 0:
            return None
        seconds_left = None if self.total.max_seconds is None else self.total.max_seconds - self.seconds
        if seconds_left is not None and seconds_left <= 0:
            return None
        return WorkerBudget(max_steps=steps_left, max_tokens=tokens_left, max_seconds=seconds_left)

    def consume(self, outcome: WorkerOutcome, elapsed_seconds: float) -> None:
        self.steps += outcome.steps
        self.tokens += outcome.tokens
        self.seconds += max(0.0, elapsed_seconds)


class Supervisor:
    """Run one objective across a small budgeted worker pool with convergence checks."""

    def __init__(
        self,
        worker: Worker,
        *,
        limits: SupervisorLimits | None = None,
        budgets: Sequence[WorkerBudget] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(worker, Worker):
            raise SupervisorError("worker must implement the Worker protocol")
        resolved_limits = SupervisorLimits() if limits is None else limits
        if not isinstance(resolved_limits, SupervisorLimits):
            raise SupervisorError("supervisor limits must be a SupervisorLimits value")
        if not callable(clock):
            raise SupervisorError("supervisor clock must be callable")
        if budgets is None:
            budget_list = [WorkerBudget() for _ in range(resolved_limits.max_workers)]
        else:
            if isinstance(budgets, (str, bytes)):
                raise SupervisorError("worker budgets must be a sequence of WorkerBudget")
            budget_list = list(budgets)
            if not budget_list:
                raise SupervisorError("at least one worker budget is required")
            for budget in budget_list:
                if not isinstance(budget, WorkerBudget):
                    raise SupervisorError("worker budgets must be WorkerBudget values")
            if len(budget_list) > resolved_limits.max_workers:
                raise SupervisorError(f"at most {resolved_limits.max_workers} worker budgets are allowed")
        self._worker = worker
        self._limits = resolved_limits
        self._worker_ids = tuple(f"worker-{index}" for index in range(1, len(budget_list) + 1))
        self._accounts = [_BudgetAccount(total=budget) for budget in budget_list]
        self._clock = clock
        self._tasks: set[asyncio.Task[WorkerRun]] = set()
        self._running = False
        self._cancelled = False

    @property
    def limits(self) -> SupervisorLimits:
        """The configured limits."""
        return self._limits

    @property
    def worker_ids(self) -> tuple[str, ...]:
        """Stable worker ids, in configuration order."""
        return self._worker_ids

    @property
    def max_workers(self) -> int:
        """The number of configured workers."""
        return len(self._worker_ids)

    @property
    def running(self) -> bool:
        """True while a run is in progress."""
        return self._running

    @property
    def cancelled(self) -> bool:
        """True once cancel() was called for the current or last run."""
        return self._cancelled

    def cancel(self) -> None:
        """Request cancellation of the running supervisor and its workers."""
        self._cancelled = True
        for task in tuple(self._tasks):
            task.cancel()

    async def run(self, objective: str) -> SupervisorResult:
        """Run the objective until convergence, failure, stall or cancellation.

        Args:
            objective: The shared objective handed to every worker each round.

        Returns:
            A SupervisorResult with the terminal status, round count and every
            WorkerRun in execution order.

        Raises:
            SupervisorError: If a run is already in progress or the objective is blank.
        """
        if self._running:
            raise SupervisorError("supervisor is already running")
        objective_text = _required_text(objective, "supervisor objective")
        self._running = True
        self._cancelled = False
        runs: list[WorkerRun] = []
        try:
            return await self._run_rounds(objective_text, runs)
        finally:
            for task in tuple(self._tasks):
                task.cancel()
            self._tasks.clear()
            self._running = False

    async def _run_rounds(self, objective: str, runs: list[WorkerRun]) -> SupervisorResult:
        limits = self._limits
        previous_signature: tuple[tuple[str, str], ...] | None = None
        stable_rounds = 0
        rounds = 0
        for round_index in range(1, limits.max_rounds + 1):
            if self._cancelled:
                return SupervisorResult(
                    status=SupervisorStatus.CANCELLED,
                    rounds=rounds,
                    runs=tuple(runs),
                    reason="cancelled before the next round",
                )
            round_runs: list[WorkerRun] = []
            assignments: list[WorkerAssignment] = []
            for worker_id, account in zip(self._worker_ids, self._accounts, strict=True):
                remaining = account.remaining()
                if remaining is None:
                    round_runs.append(
                        WorkerRun(
                            worker_id=worker_id,
                            round=round_index,
                            error="worker budget exhausted",
                            rejected=True,
                        )
                    )
                    continue
                assignments.append(
                    WorkerAssignment(
                        worker_id=worker_id,
                        objective=objective,
                        round=round_index,
                        budget=remaining,
                    )
                )
            if assignments:
                round_runs.extend(await self._gather_assignments(assignments))
            round_runs.sort(key=lambda run: run.worker_id)
            runs.extend(round_runs)
            rounds = round_index
            if self._cancelled:
                return SupervisorResult(
                    status=SupervisorStatus.CANCELLED,
                    rounds=rounds,
                    runs=tuple(runs),
                    reason=f"cancelled during round {round_index}",
                )
            usable = [run for run in round_runs if run.ok and run.outcome is not None]
            if not usable:
                return SupervisorResult(
                    status=SupervisorStatus.FAILED,
                    rounds=rounds,
                    runs=tuple(runs),
                    reason=f"no worker produced a usable outcome in round {round_index}",
                )
            failures = len(round_runs) - len(usable)
            done_outcomes = [run.outcome for run in usable if run.outcome is not None]
            if all(outcome.done for outcome in done_outcomes):
                if failures:
                    reason = f"converged with {failures} failed worker(s) after {round_index} round(s)"
                else:
                    reason = f"every worker reported done after {round_index} round(s)"
                return SupervisorResult(
                    status=SupervisorStatus.CONVERGED,
                    rounds=rounds,
                    runs=tuple(runs),
                    reason=reason,
                )
            if len(usable) == len(self._worker_ids):
                normalized = {_normalize_summary(run.summary) for run in usable}
                if len(normalized) == 1 and normalized != {""}:
                    return SupervisorResult(
                        status=SupervisorStatus.CONVERGED,
                        rounds=rounds,
                        runs=tuple(runs),
                        reason=f"workers agree on the same summary after {round_index} round(s)",
                    )
            signature = tuple((run.worker_id, _normalize_summary(run.summary)) for run in usable)
            if previous_signature is not None and signature == previous_signature:
                stable_rounds += 1
                if stable_rounds >= limits.stall_rounds:
                    return SupervisorResult(
                        status=SupervisorStatus.STALLED,
                        rounds=rounds,
                        runs=tuple(runs),
                        reason=f"worker summaries unchanged for {stable_rounds + 1} rounds",
                    )
            else:
                stable_rounds = 0
            previous_signature = signature
        return SupervisorResult(
            status=SupervisorStatus.ROUND_LIMIT,
            rounds=rounds,
            runs=tuple(runs),
            reason=f"no convergence after {limits.max_rounds} rounds",
        )

    async def _gather_assignments(self, assignments: Sequence[WorkerAssignment]) -> list[WorkerRun]:
        tasks = [
            asyncio.create_task(
                self._run_worker(assignment),
                name=f"supervisor:{assignment.worker_id}:{assignment.round}",
            )
            for assignment in assignments
        ]
        self._tasks.update(tasks)
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.difference_update(tasks)
        runs: list[WorkerRun] = []
        for assignment, result in zip(assignments, results, strict=True):
            if isinstance(result, WorkerRun):
                runs.append(result)
            elif isinstance(result, asyncio.CancelledError):
                runs.append(
                    WorkerRun(
                        worker_id=assignment.worker_id,
                        round=assignment.round,
                        error="worker cancelled",
                    )
                )
            else:
                runs.append(
                    WorkerRun(
                        worker_id=assignment.worker_id,
                        round=assignment.round,
                        error=f"{type(result).__name__}: {result}",
                    )
                )
        return runs

    async def _run_worker(self, assignment: WorkerAssignment) -> WorkerRun:
        budget = assignment.budget
        timeout = budget.max_seconds
        started = self._clock()
        try:
            if timeout is None:
                outcome = await self._worker.run(assignment)
            else:
                outcome = await asyncio.wait_for(self._worker.run(assignment), timeout)
        except TimeoutError:
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                error=f"worker exceeded its time budget ({budget.max_seconds}s)",
            )
        except asyncio.CancelledError:
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                error="worker cancelled",
            )
        except Exception as exc:  # noqa: BLE001 - worker failures become records, not crashes
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                error=f"{type(exc).__name__}: {exc}",
            )
        elapsed = max(0.0, self._clock() - started)
        if not isinstance(outcome, WorkerOutcome):
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                error="worker returned an invalid outcome",
            )
        account = self._accounts[self._worker_ids.index(assignment.worker_id)]
        account.consume(outcome, elapsed)
        if outcome.steps > budget.max_steps:
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                outcome=outcome,
                error=f"worker exceeded its step budget ({outcome.steps} > {budget.max_steps})",
            )
        if budget.max_tokens is not None and outcome.tokens > budget.max_tokens:
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                outcome=outcome,
                error=f"worker exceeded its token budget ({outcome.tokens} > {budget.max_tokens})",
            )
        if timeout is not None and elapsed > timeout:
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                outcome=outcome,
                error=f"worker exceeded its time budget ({elapsed:.3f}s > {timeout}s)",
            )
        if outcome.failed or outcome.error:
            return WorkerRun(
                worker_id=assignment.worker_id,
                round=assignment.round,
                outcome=outcome,
                error=outcome.error or "worker reported failure",
            )
        return WorkerRun(
            worker_id=assignment.worker_id,
            round=assignment.round,
            outcome=outcome,
        )


class SubagentServiceWorker:
    """Adapt a SubagentService to the Worker protocol.

    Outcomes carry the child run's measured usage (provider-reported tokens,
    latency, retries and steps), so supervisor step/token budgets are checked
    against real provider counters rather than estimates.
    """

    def __init__(self, service: SubagentService) -> None:
        if not isinstance(service, SubagentService):
            raise SupervisorError("subagent worker requires a SubagentService")
        self._service = service

    @property
    def service(self) -> SubagentService:
        """The wrapped subagent service."""
        return self._service

    async def run(self, assignment: WorkerAssignment) -> WorkerOutcome:
        """Run one child agent and translate its result into a WorkerOutcome."""
        result: SubagentResult = await self._service.run(
            assignment.objective,
            description=assignment.objective,
        )
        usage = result.usage
        return WorkerOutcome(
            summary=result.text,
            steps=usage.steps if usage is not None else 0,
            tokens=usage.total_tokens if usage is not None else 0,
            done=not result.is_error,
            failed=result.is_error,
            error=result.error or None,
            usage=usage,
        )


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_STEPS",
    "DEFAULT_STALL_ROUNDS",
    "MAX_WORKERS",
    "SubagentServiceWorker",
    "Supervisor",
    "SupervisorError",
    "SupervisorLimits",
    "SupervisorResult",
    "SupervisorStatus",
    "Worker",
    "WorkerAssignment",
    "WorkerBudget",
    "WorkerOutcome",
    "WorkerRun",
]
