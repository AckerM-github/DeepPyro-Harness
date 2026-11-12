"""Batch pipeline: ordering, rolling concurrency, barriers, errors, retention."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.core.protocols import ToolResult
from deeppyro.tools import ToolCallRequest, ToolRegistry, run_tool_batch
from deeppyro.tools.base import BaseTool
from tests.tools.helpers import make_context


class FakeTool(BaseTool):
    """Deterministic tool used to observe scheduling order."""

    def __init__(
        self,
        name: str,
        *,
        parallel_safe: bool = True,
        delay: float = 0.0,
        result: ToolResult | None = None,
        raises: BaseException | None = None,
        events: list[tuple[str, str]] | None = None,
        state: dict[str, int] | None = None,
    ) -> None:
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.parallel_safe = parallel_safe
        self.delay = delay
        self.result = result
        self.raises = raises
        self.events = events
        self.state = state

    async def run(self, args: dict[str, Any], ctx) -> ToolResult:  # type: ignore[no-untyped-def]
        if self.state is not None:
            self.state["current"] += 1
            self.state["max"] = max(self.state["max"], self.state["current"])
        if self.events is not None:
            self.events.append(("start", self.name))
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.raises is not None:
                raise self.raises
            return self.result or ToolResult(text=f"{self.name} done")
        finally:
            if self.state is not None:
                self.state["current"] -= 1
            if self.events is not None:
                self.events.append(("end", self.name))


def request(call_id: str, name: str, arguments: str = "{}") -> ToolCallRequest:
    return ToolCallRequest(call_id=call_id, name=name, arguments=arguments)


async def run(
    registry: ToolRegistry, requests: list[ToolCallRequest], *, max_concurrency: int = 8, services=None
):  # type: ignore[no-untyped-def]
    return await run_tool_batch(
        requests,
        registry=registry,
        ctx_factory=lambda req: make_context(Path.cwd(), services=services, call_id=req.call_id),
        max_concurrency=max_concurrency,
    )


async def test_outcomes_follow_request_order_not_completion_order() -> None:
    registry = ToolRegistry(
        [
            FakeTool("slow", delay=0.05),
            FakeTool("fast", delay=0.0),
            FakeTool("medium", delay=0.02),
        ]
    )
    outcomes = await run(registry, [request("c1", "slow"), request("c2", "fast"), request("c3", "medium")])
    assert [outcome.call_id for outcome in outcomes] == ["c1", "c2", "c3"]
    assert [outcome.name for outcome in outcomes] == ["slow", "fast", "medium"]
    assert all(outcome.duration_ms >= 0 for outcome in outcomes)


async def test_rolling_concurrency_respects_max_concurrency() -> None:
    state = {"current": 0, "max": 0}
    registry = ToolRegistry([FakeTool("worker", delay=0.03, state=state)])
    requests = [request(f"c{index}", "worker") for index in range(8)]
    outcomes = await run(registry, requests, max_concurrency=3)
    assert len(outcomes) == 8
    assert state["max"] == 3


async def test_sequential_tool_is_a_barrier() -> None:
    events: list[tuple[str, str]] = []
    registry = ToolRegistry(
        [
            FakeTool("parallel", delay=0.05, events=events),
            FakeTool("sequential", parallel_safe=False, delay=0.01, events=events),
        ]
    )
    outcomes = await run(
        registry,
        [
            request("c1", "parallel"),
            request("c2", "parallel"),
            request("c3", "sequential"),
            request("c4", "parallel"),
        ],
    )
    assert len(outcomes) == 4
    names = [event for event in events]
    sequential_start = names.index(("start", "sequential"))
    sequential_end = names.index(("end", "sequential"))
    # Both earlier parallel calls finished before the sequential tool started.
    assert names[:sequential_start].count(("end", "parallel")) == 2
    # The later parallel call starts only after the barrier finished.
    assert names.index(("start", "parallel"), sequential_end + 1) > sequential_end


async def test_unknown_tool_is_an_error_result() -> None:
    outcomes = await run(ToolRegistry(), [request("c1", "nope")])
    assert outcomes[0].result.is_error is True
    assert 'unknown tool "nope"' in outcomes[0].result.text


async def test_bad_arguments_are_error_results() -> None:
    registry = ToolRegistry([FakeTool("tool")])
    outcomes = await run(
        registry,
        [request("c1", "tool", "{not json"), request("c2", "tool", "[1, 2]")],
    )
    assert outcomes[0].result.is_error is True
    assert "invalid JSON arguments" in outcomes[0].result.text
    assert outcomes[1].result.is_error is True
    assert "must be a JSON object" in outcomes[1].result.text


async def test_tool_errors_permission_denials_and_bugs_become_error_results() -> None:
    registry = ToolRegistry(
        [
            FakeTool("tool_error", raises=ToolError("tool broke")),
            FakeTool("denied", raises=PermissionDenied("no writes")),
            FakeTool("bug", raises=RuntimeError("kaboom")),
            FakeTool("wrong", result="not a ToolResult"),  # type: ignore[arg-type]
        ]
    )
    outcomes = await run(
        registry,
        [
            request("c1", "tool_error"),
            request("c2", "denied"),
            request("c3", "bug"),
            request("c4", "wrong"),
        ],
    )
    assert all(outcome.result.is_error for outcome in outcomes)
    assert outcomes[0].result.text == "Error: tool broke"
    assert outcomes[1].result.text == "Error: no writes"
    assert "kaboom" in outcomes[2].result.text
    assert outcomes[2].result.meta is not None and "traceback" in outcomes[2].result.meta
    assert "not ToolResult" in outcomes[3].result.text


async def test_cancellation_propagates() -> None:
    registry = ToolRegistry([FakeTool("cancelled", raises=asyncio.CancelledError())])
    with pytest.raises(asyncio.CancelledError):
        await run(registry, [request("c1", "cancelled")])


async def test_max_concurrency_must_be_positive() -> None:
    with pytest.raises(ValueError):
        await run(ToolRegistry(), [], max_concurrency=0)


async def test_long_output_is_truncated_and_spilled(tmp_path: Path) -> None:
    text = "HEAD" + "x" * 200 + "TAIL"
    registry = ToolRegistry([FakeTool("verbose", result=ToolResult(text=text))])
    spill_dir = tmp_path / "spill"
    outcomes = await run(
        registry,
        [request("call-1", "verbose")],
        services={"max_output_chars": 50, "spill_dir": str(spill_dir)},
    )
    result = outcomes[0].result
    assert result.is_error is False
    assert "HEAD" in result.text and "TAIL" in result.text
    assert "... [truncated 158 chars] ..." in result.text
    assert len(result.text) == 50 + len("... [truncated 158 chars] ...")
    assert result.meta is not None
    assert result.meta["truncated"] is True
    assert result.meta["omittedChars"] == 158
    spill_path = Path(result.meta["spillPath"])
    assert spill_path.is_file()
    assert spill_path.read_text(encoding="utf-8") == text


async def test_short_output_is_not_truncated(tmp_path: Path) -> None:
    registry = ToolRegistry([FakeTool("tiny", result=ToolResult(text="ok"))])
    outcomes = await run(registry, [request("c1", "tiny")], services={"spill_dir": str(tmp_path)})
    assert outcomes[0].result.text == "ok"
    assert outcomes[0].result.meta is not None
    assert "truncated" not in outcomes[0].result.meta


async def test_duration_is_recorded() -> None:
    registry = ToolRegistry([FakeTool("slow", delay=0.05)])
    started = time.monotonic()
    outcomes = await run(registry, [request("c1", "slow")])
    assert outcomes[0].duration_ms >= 40
    assert time.monotonic() - started >= 0.05
