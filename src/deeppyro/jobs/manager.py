"""In-process background job registry.

A job is an asyncio task running a caller supplied producer. The service owns
the lifecycle, the output buffer and the durable records; producers only write
output through the handle and return an exit code. A failing or cancelled
producer never crashes the service: it settles the job with a terminal status
and the settlement record is still appended.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from deeppyro.core.ids import now_ms

from .types import TERMINAL_STATUSES, JobResult

RecordCallback = Callable[[str, dict[str, Any]], Any]
Runner = Callable[["JobHandle"], Any]

#: How long kill() waits for a cancelled producer to settle before forcing a
#: settlement itself. The producer should observe cancellation quickly.
KILL_SETTLE_TIMEOUT = 5.0


class JobHandle:
    """Live handle for one background job.

    Producers call write() to append output and return an exit code from the
    runner coroutine. Consumers call wait() for a bounded snapshot.
    """

    def __init__(
        self,
        *,
        id: str,
        kind: str,
        label: str,
        owner: str = "",
        call_id: str = "",
        started_at: int = 0,
    ) -> None:
        self.id = id
        self.kind = kind
        self.label = label
        self.owner = owner
        self.call_id = call_id
        self.started_at = started_at
        self.status = "running"
        self.finished_at: int | None = None
        self.exit_code: int | None = None
        self._chunks: list[str] = []
        self._done = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None
        self._record: RecordCallback | None = None
        self._timeout_ms: int | None = None
        self._cancel_reason = ""
        self._settled = False
        self._cause = ""
        self._detail = ""

    # -- state ---------------------------------------------------------
    @property
    def output(self) -> str:
        """All output the producer has written so far."""
        return "".join(self._chunks)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def cancel_reason(self) -> str:
        return self._cancel_reason

    @property
    def cause(self) -> str:
        """Why the job settled: producer, kill or timeout."""
        return self._cause

    @property
    def detail(self) -> str:
        """Short human-readable settlement detail (for example exit code)."""
        return self._detail

    def write(self, chunk: str) -> None:
        """Append a chunk of output; writes after settlement are ignored."""
        if not chunk or self._settled:
            return
        self._chunks.append(chunk if isinstance(chunk, str) else str(chunk))

    def snapshot(self, *, timed_out: bool = False) -> JobResult:
        """Return an immutable snapshot of the current state."""
        return JobResult(
            id=self.id,
            status=self.status,
            exit_code=self.exit_code,
            output=self.output,
            started_at=self.started_at,
            finished_at=self.finished_at,
            timed_out=timed_out,
            cause=self._cause,
            detail=self._detail,
        )

    async def wait(self, timeout: float | None = None) -> JobResult:
        """Wait for settlement, or return the current snapshot after timeout."""
        timed_out = False
        if not self._settled:
            try:
                if timeout is None:
                    await self._done.wait()
                else:
                    await asyncio.wait_for(self._done.wait(), max(0.0, float(timeout)))
            except TimeoutError:
                timed_out = True
        return self.snapshot(timed_out=timed_out)

    def __repr__(self) -> str:
        return f"<JobHandle id={self.id!r} kind={self.kind!r} status={self.status!r}>"


class JobsService:
    """Registry and lifecycle owner for background jobs in one runtime."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobHandle] = {}

    # -- lookup --------------------------------------------------------
    def get(self, job_id: str) -> JobHandle | None:
        """Return the handle for job_id, or None when it is unknown."""
        return self._jobs.get(job_id)

    def list(self) -> list[JobHandle]:
        """All known jobs in creation order."""
        return list(self._jobs.values())

    def output(self, job_id: str) -> str:
        """Accumulated output for job_id, or an empty string when unknown."""
        handle = self._jobs.get(job_id)
        return handle.output if handle is not None else ""

    async def wait(self, job_id: str, timeout: float | None = None) -> JobResult | None:
        """Wait for one job; None when the id is unknown."""
        handle = self._jobs.get(job_id)
        if handle is None:
            return None
        return await handle.wait(timeout)

    # -- lifecycle -----------------------------------------------------
    def start(
        self,
        kind: str,
        label: str,
        runner: Runner,
        *,
        owner: str = "",
        call_id: str = "",
        record: RecordCallback | None = None,
        timeout_ms: int | None = None,
    ) -> JobHandle:
        """Create and schedule one background job.

        The job/registered record is appended before the producer task starts.
        timeout_ms optionally bounds the producer; background shell jobs pass
        None because their work must not be killed by the tool timeout.
        """
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("kind must be a non-empty string")
        if not isinstance(label, str):
            raise TypeError("label must be a string")
        if not callable(runner):
            raise TypeError("runner must be callable")
        if timeout_ms is not None and (
            isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1
        ):
            raise ValueError("timeout_ms must be a positive integer")

        job_id = f"{kind.strip()}-{uuid4().hex[:16]}"
        handle = JobHandle(
            id=job_id,
            kind=kind.strip(),
            label=label,
            owner=owner,
            call_id=call_id,
            started_at=now_ms(),
        )
        handle._record = record
        handle._timeout_ms = timeout_ms
        self._jobs[job_id] = handle
        self._emit_record(handle, settled=False)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:  # pragma: no cover - service is asyncio only
            raise RuntimeError("JobsService.start() requires a running event loop") from exc
        task = loop.create_task(self._run(handle, runner))
        handle._task = task
        task.add_done_callback(lambda done: self._on_task_done(handle, done))
        return handle

    async def kill(self, job_id: str, reason: str | None = None) -> bool:
        """Request cancellation and wait briefly for the job to settle.

        Returns False for an unknown job and True otherwise, including for a
        job that already reached a terminal state.
        """
        handle = self._jobs.get(job_id)
        if handle is None:
            return False
        if handle.finished:
            return True
        handle._cancel_reason = reason or ""
        task = handle._task
        if task is not None:
            task.cancel()
        try:
            await asyncio.wait_for(handle.wait(), KILL_SETTLE_TIMEOUT)
        except TimeoutError:
            detail = "killed before exit"
            if handle.cancel_reason:
                detail = f"{detail}; {handle.cancel_reason}"
            self._settle(
                handle,
                status="cancelled",
                cause="kill",
                detail=detail,
                exit_code=handle.exit_code,
            )
        return True

    async def cancel_owner(self, owner: str) -> int:
        """Cancel every running job owned by owner; returns the count."""
        targets = [handle for handle in self._jobs.values() if handle.running and handle.owner == owner]
        if targets:
            await asyncio.gather(*(self.kill(handle.id, "owner cancelled") for handle in targets))
        return len(targets)

    async def cancel_all(self) -> int:
        """Cancel every running job; returns the number of jobs targeted."""
        targets = [handle for handle in self._jobs.values() if handle.running]
        if targets:
            await asyncio.gather(*(self.kill(handle.id, "job registry cancelled") for handle in targets))
        return len(targets)

    # -- producer task -------------------------------------------------
    async def _run(self, handle: JobHandle, runner: Runner) -> None:
        status = "completed"
        cause = "producer"
        detail = ""
        exit_code: int | None = None
        try:
            if handle._timeout_ms is not None:
                exit_code = await asyncio.wait_for(runner(handle), handle._timeout_ms / 1000)
            else:
                exit_code = await runner(handle)
            if exit_code is not None:
                detail = f"exit code: {exit_code}"
        except asyncio.CancelledError:
            status = "cancelled"
            cause = "kill"
            detail = "killed before exit"
            if handle.cancel_reason:
                detail = f"{detail}; {handle.cancel_reason}"
        except TimeoutError:
            status = "failed"
            cause = "timeout"
            detail = f"timed out after {handle._timeout_ms}ms"
            handle.write(f"\nError: {detail}\n")
        except Exception as exc:  # noqa: BLE001 - a producer failure settles the job
            status = "failed"
            cause = "producer"
            detail = f"{type(exc).__name__}: {exc}"
            handle.write(f"\nError: {exc}\n")
        finally:
            self._settle(
                handle,
                status=status,
                cause=cause,
                detail=detail,
                exit_code=exit_code,
            )

    def _on_task_done(self, handle: JobHandle, task: asyncio.Task[Any]) -> None:
        """Fallback settlement for a task cancelled before its coroutine ran."""
        if handle._settled:
            return
        status = "cancelled"
        cause = "kill"
        detail = "killed before exit"
        exit_code: int | None = None
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                status = "failed"
                cause = "producer"
                detail = f"{type(error).__name__}: {error}"
            else:
                status = "completed"
                cause = "producer"
        self._settle(
            handle,
            status=status,
            cause=cause,
            detail=detail,
            exit_code=exit_code,
        )

    def _settle(
        self,
        handle: JobHandle,
        *,
        status: str,
        cause: str,
        detail: str,
        exit_code: int | None,
    ) -> bool:
        """Settle exactly once and append the job/settled record."""
        if handle._settled:
            return False
        handle._settled = True
        handle.status = status
        handle.exit_code = exit_code
        handle.finished_at = now_ms()
        handle._cause = cause
        handle._detail = detail
        self._emit_record(handle, settled=True)
        handle._done.set()
        return True

    # -- durable records -----------------------------------------------
    def _emit_record(self, handle: JobHandle, *, settled: bool) -> None:
        """Append a lifecycle record through the producer supplied callback.

        A broken recorder must never take the service down, so both sync
        exceptions and awaitable results are ignored here.
        """
        callback = handle._record
        if callback is None:
            return
        payload: dict[str, Any] = {
            "jobId": handle.id,
            "owner": handle.owner,
            "kind": handle.kind,
            "label": handle.label,
            "startedAt": handle.started_at,
            "toolCall": {"callId": handle.call_id, "rootCallId": handle.call_id},
        }
        if settled:
            payload.update(
                {
                    "finishedAt": handle.finished_at,
                    "status": handle.status,
                    "cause": handle._cause,
                    "producerReported": True,
                    "detail": handle._detail,
                }
            )
        try:
            result = callback("job/settled" if settled else "job/registered", payload)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
        except Exception:  # noqa: BLE001, S110 - recording is best effort
            pass


__all__ = ["KILL_SETTLE_TIMEOUT", "JobHandle", "JobsService", "RecordCallback", "Runner"]
