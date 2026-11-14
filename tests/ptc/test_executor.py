"""Tests for NestedDispatcher: bindings, records, ordering and quotas."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.ptc import NestedDispatcher, PtcBindingError
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.registry import ToolRegistry

from .helpers import make_context


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo a value."
    parameters: ClassVar[dict[str, Any]] = make_parameters({"value": {"type": "string"}}, required=["value"])
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(text="echo:" + str(args["value"]), meta={"value": args["value"]})


class BoomTool(BaseTool):
    name = "boom"
    description = "Always fail."
    parameters: ClassVar[dict[str, Any]] = make_parameters({})
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise ToolError("boom")


class DeniedTool(BaseTool):
    name = "denied"
    description = "Permission denied."
    parameters: ClassVar[dict[str, Any]] = make_parameters({})
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise PermissionDenied("no way")


class ImageTool(BaseTool):
    name = "image"
    description = "Return an image part."
    parameters: ClassVar[dict[str, Any]] = make_parameters({})
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            text="attached",
            parts=[{"type": "image", "attachmentId": "att-1", "mediaType": "image/png"}],
        )


class Tracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.exclusive_saw_active = -1

    def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        self.active -= 1


def _make_slow_tool(tracker: Tracker, name: str = "slow", *, parallel: bool = True) -> BaseTool:
    class SlowTool(BaseTool):
        description = "Sleep while tracking concurrency."
        parameters: ClassVar[dict[str, Any]] = make_parameters({"delay": {"type": "number"}}, required=[])
        parallel_safe = parallel

        async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            tracker.exclusive_saw_active = tracker.active
            tracker.enter()
            try:
                await asyncio.sleep(float(args.get("delay", 0.05)))
            finally:
                tracker.leave()
            return ToolResult(text="done")

    SlowTool.name = name
    return SlowTool()


def _dispatcher(
    tmp_path: Path,
    *tools: BaseTool,
    events: list[tuple[str, dict[str, Any]]] | None = None,
    max_parallel: int = 10,
    rate: int = 1024,
    timeout_ms: int = 120_000,
) -> NestedDispatcher:
    ctx = make_context(tmp_path, events=events)
    return NestedDispatcher(
        root_call_id=ctx.call_id,
        registry=ToolRegistry(list(tools)),
        ctx=ctx,
        max_parallel_sub_calls=max_parallel,
        max_sub_calls_per_minute=rate,
        timeout_ms=timeout_ms,
    )


def test_bindings_exclude_reserved_tools(tmp_path: Path) -> None:
    class RunTool(EchoTool):
        name = "run_code"
        reserved_tool = True

    class AskTool(EchoTool):
        name = "ask_user_question"

    dispatcher = _dispatcher(tmp_path, EchoTool(), RunTool(), AskTool())
    assert set(dispatcher.bindings) == {"echo"}


async def test_dispatch_records_sub_call_shape(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    dispatcher = _dispatcher(tmp_path, EchoTool(), events=events)
    outcome = await dispatcher.dispatch("echo", {"value": "hi"})
    assert outcome.result.text == "echo:hi"
    assert [kind for kind, _ in events] == ["tool/ptc-dispatch-start", "tool/ptc-dispatch"]
    start, settled = events[0][1], events[1][1]
    assert start["subCallId"] == "call_ptc_test:ptc:1"
    assert settled["subCallId"] == "call_ptc_test:ptc:1"
    assert settled["isError"] is False
    assert settled["meta"] == {"value": "hi"}
    assert dispatcher.call_count == 1


async def test_parallel_calls_are_capped(tmp_path: Path) -> None:
    tracker = Tracker()
    dispatcher = _dispatcher(tmp_path, _make_slow_tool(tracker), max_parallel=2)
    await asyncio.gather(*(dispatcher.dispatch("slow", {"delay": 0.08}) for _ in range(4)))
    assert tracker.peak == 2


async def test_non_parallel_tool_is_an_exclusive_barrier(tmp_path: Path) -> None:
    tracker = Tracker()
    slow = _make_slow_tool(tracker, name="slow", parallel=True)
    exclusive = _make_slow_tool(tracker, name="exclusive", parallel=False)
    dispatcher = _dispatcher(tmp_path, slow, exclusive, max_parallel=4)
    running = [asyncio.create_task(dispatcher.dispatch("slow", {"delay": 0.1})) for _ in range(3)]
    while tracker.active == 0:
        await asyncio.sleep(0)
    await dispatcher.dispatch("exclusive", {"delay": 0})
    assert tracker.exclusive_saw_active == 0
    await asyncio.gather(*running)


async def test_quota_exceeded_is_fatal(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, EchoTool(), rate=1, timeout_ms=60_000, max_parallel=1)
    await dispatcher.dispatch("echo", {"value": "one"})
    with pytest.raises(PtcBindingError) as first:
        await dispatcher.dispatch("echo", {"value": "two"})
    assert first.value.code == "QUOTA_EXCEEDED"
    assert dispatcher.fatal is not None
    assert dispatcher.fatal.kind == "exception"
    assert "quota exceeded" in dispatcher.fatal.message
    with pytest.raises(PtcBindingError) as third:
        await dispatcher.dispatch("echo", {"value": "three"})
    assert third.value.code == "QUOTA_EXCEEDED"


async def test_tool_error_becomes_binding_error(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, BoomTool())
    with pytest.raises(PtcBindingError) as raised:
        await dispatcher.dispatch("boom", {})
    assert raised.value.code == "TOOL_ERROR"
    assert raised.value.reason == "boom"


async def test_permission_denied_becomes_binding_error(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, DeniedTool())
    with pytest.raises(PtcBindingError) as raised:
        await dispatcher.dispatch("denied", {})
    assert raised.value.code == "PERMISSION_DENIED"


async def test_unknown_tool_becomes_binding_error(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, EchoTool())
    with pytest.raises(PtcBindingError) as raised:
        await dispatcher.dispatch("nope", {})
    assert raised.value.code == "UNKNOWN_TOOL"
    assert "unknown tool" in raised.value.reason


async def test_successful_image_parts_are_deferred(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, ImageTool())
    await dispatcher.dispatch("image", {})
    assert dispatcher.deferred_parts == [{"type": "image", "attachmentId": "att-1", "mediaType": "image/png"}]
    assert dispatcher.deferred_images == dispatcher.deferred_parts


async def test_binding_callable_returns_text(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path, EchoTool())
    value = await dispatcher.bindings["echo"]({"value": "x"})
    assert value == "echo:x"


def test_parallel_limit_must_be_positive(tmp_path: Path) -> None:
    ctx = make_context(tmp_path)
    with pytest.raises(ValueError):
        NestedDispatcher(
            root_call_id=ctx.call_id,
            registry=ToolRegistry(),
            ctx=ctx,
            max_parallel_sub_calls=0,
        )
