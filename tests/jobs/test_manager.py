"""JobsService lifecycle: records, output, settlement and cancellation."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deeppyro.jobs import TERMINAL_STATUSES, JobHandle, JobsService


def recorder(events: list[tuple[str, dict[str, Any]]]):
    """Return a record callback that appends (type, payload) pairs."""

    def callback(event: str, payload: dict[str, Any]) -> None:
        events.append((event, payload))

    return callback


async def _never_finishes(handle: JobHandle) -> int:
    handle.write("started\n")
    try:
        await asyncio.Event().wait()
    finally:
        handle.write("cleanup\n")
    return 0


async def test_start_emits_exact_registered_and_settled_records() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    service = JobsService()

    async def runner(handle: JobHandle) -> int:
        handle.write("hello ")
        handle.write("world")
        return 0

    handle = service.start(
        "pwsh",
        "echo hi",
        runner,
        owner="session-1",
        call_id="call_1",
        record=recorder(events),
    )
    assert handle.running is True
    assert handle.finished is False
    assert handle.id.startswith("pwsh-")
    assert len(handle.id) == len("pwsh-") + 16
    assert handle.started_at > 0
    assert service.get(handle.id) is handle
    assert service.list() == [handle]
    assert [event for event, _ in events] == ["job/registered"]

    result = await handle.wait(5)
    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.output == "hello world"
    assert result.finished_at is not None
    assert result.cause == "producer"
    assert result.detail == "exit code: 0"
    assert resource_status(handle) == "completed"

    assert [event for event, _ in events] == ["job/registered", "job/settled"]
    registered = events[0][1]
    assert registered == {
        "jobId": handle.id,
        "owner": "session-1",
        "kind": "pwsh",
        "label": "echo hi",
        "startedAt": handle.started_at,
        "toolCall": {"callId": "call_1", "rootCallId": "call_1"},
    }
    settled = events[1][1]
    assert set(settled) == {
        "jobId",
        "owner",
        "kind",
        "label",
        "startedAt",
        "toolCall",
        "finishedAt",
        "status",
        "cause",
        "producerReported",
        "detail",
    }
    assert settled["jobId"] == handle.id
    assert settled["status"] == "completed"
    assert settled["cause"] == "producer"
    assert settled["producerReported"] is True
    assert settled["detail"] == "exit code: 0"
    assert settled["finishedAt"] == handle.finished_at


def resource_status(handle: JobHandle) -> str:
    """Read a handle status while keeping the assertion readable."""
    return handle.status


async def test_output_and_lookup_helpers() -> None:
    service = JobsService()

    async def runner(handle: JobHandle) -> int:
        handle.write("chunk-1")
        handle.write("chunk-2")
        return 7

    handle = service.start("bash", "produce", runner)
    result = await service.wait(handle.id, 5)
    assert result is not None
    assert result.status == "completed"
    assert result.exit_code == 7
    assert service.output(handle.id) == "chunk-1chunk-2"
    assert service.output("missing-job") == ""
    assert await service.wait("missing-job") is None


async def test_nonzero_exit_is_still_completed() -> None:
    service = JobsService()

    async def runner(handle: JobHandle) -> int:
        handle.write("before failure")
        return 3

    handle = service.start("pwsh", "exit three", runner)
    result = await handle.wait(5)
    assert result.status == "completed"
    assert result.exit_code == 3
    assert result.cause == "producer"
    assert result.detail == "exit code: 3"


async def test_runner_exception_settles_failed_and_keeps_service_usable() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    service = JobsService()

    async def broken(handle: JobHandle) -> int:
        handle.write("partial")
        raise RuntimeError("boom")

    handle = service.start("pwsh", "broken", broken, record=recorder(events))
    result = await handle.wait(5)
    assert result.status == "failed"
    assert result.cause == "producer"
    assert result.exit_code is None
    assert "RuntimeError: boom" in result.detail
    assert "Error: boom" in result.output
    assert events[-1][1]["status"] == "failed"
    assert events[-1][1]["producerReported"] is True

    async def healthy(handle: JobHandle) -> int:
        handle.write("ok")
        return 0

    second = service.start("pwsh", "healthy", healthy)
    assert (await second.wait(5)).status == "completed"


async def test_kill_cancels_and_records_reason() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    service = JobsService()
    handle = service.start("pwsh", "long task", _never_finishes, record=recorder(events))
    await asyncio.sleep(0)
    assert handle.running is True

    assert await service.kill(handle.id, "user asked") is True
    assert handle.status == "cancelled"
    assert handle.status in TERMINAL_STATUSES
    assert handle.cause == "kill"
    assert handle.cancel_reason == "user asked"
    assert "killed before exit" in handle.detail
    assert "user asked" in handle.detail
    assert events[-1][1]["status"] == "cancelled"
    assert events[-1][1]["cause"] == "kill"


async def test_kill_unknown_and_already_finished() -> None:
    service = JobsService()
    assert await service.kill("does-not-exist") is False

    async def runner(handle: JobHandle) -> int:
        return 0

    handle = service.start("pwsh", "quick", runner)
    await handle.wait(5)
    assert await service.kill(handle.id) is True
    assert handle.status == "completed"


async def test_wait_timeout_returns_running_snapshot() -> None:
    service = JobsService()
    handle = service.start("pwsh", "waiting", _never_finishes)
    result = await handle.wait(0.01)
    assert result.timed_out is True
    assert result.status == "running"
    assert result.finished_at is None
    await service.kill(handle.id)


async def test_cancel_owner_only_targets_matching_owner() -> None:
    service = JobsService()
    first = service.start("pwsh", "a1", _never_finishes, owner="owner-a")
    second = service.start("pwsh", "a2", _never_finishes, owner="owner-a")
    third = service.start("pwsh", "b1", _never_finishes, owner="owner-b")
    await asyncio.sleep(0)

    assert await service.cancel_owner("owner-a") == 2
    assert first.status == "cancelled"
    assert second.status == "cancelled"
    assert third.status == "running"
    assert await service.cancel_all() == 1
    assert third.status == "cancelled"


async def test_timeout_ms_fails_slow_producer() -> None:
    service = JobsService()

    async def slow(handle: JobHandle) -> int:
        await asyncio.sleep(5)
        return 0

    handle = service.start("pwsh", "slow", slow, timeout_ms=10)
    result = await handle.wait(5)
    assert result.status == "failed"
    assert result.cause == "timeout"
    assert "timed out after 10ms" in result.detail
    assert "Error: timed out after 10ms" in result.output


async def test_broken_recorder_does_not_break_the_job() -> None:
    service = JobsService()

    def broken_record(event: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("disk full")

    async def runner(handle: JobHandle) -> int:
        handle.write("done")
        return 0

    handle = service.start("pwsh", "record failure", runner, record=broken_record)
    result = await handle.wait(5)
    assert result.status == "completed"
    assert result.output == "done"


async def test_writes_after_settlement_are_ignored() -> None:
    service = JobsService()

    async def runner(handle: JobHandle) -> int:
        handle.write("before")
        return 0

    handle = service.start("pwsh", "late write", runner)
    await handle.wait(5)
    handle.write("after")
    assert handle.output == "before"


async def test_task_cancelled_before_producer_starts_still_settles() -> None:
    service = JobsService()
    handle = service.start("pwsh", "pre-start cancel", _never_finishes)
    assert handle._task is not None
    handle._task.cancel()
    result = await handle.wait(5)
    assert result.status in TERMINAL_STATUSES
    assert result.status == "cancelled"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "", "label": "x"},
        {"kind": "pwsh", "label": 3},
        {"kind": "pwsh", "label": "x", "timeout_ms": 0},
        {"kind": "pwsh", "label": "x", "timeout_ms": True},
    ],
)
def test_start_validates_arguments(kwargs: dict[str, Any]) -> None:
    service = JobsService()
    kind = kwargs.pop("kind")
    label = kwargs.pop("label")
    with pytest.raises((TypeError, ValueError)):
        service.start(kind, label, _never_finishes, **kwargs)
