"""Unit tests for the native plugin fiber lifecycle."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from deeppyro.api.kernel_types import FiberState
from deeppyro.kernel.context import RuntimeContext
from deeppyro.kernel.fiber import Fiber


def _context() -> RuntimeContext:
    return RuntimeContext.create_root()


async def test_mount_loads_plugin_and_becomes_active() -> None:
    ctx = _context()
    seen: list[tuple[object, object]] = []

    async def apply(child_ctx: object, config: object) -> None:
        seen.append((child_ctx, config))

    fiber = ctx.plugin({"name": "demo", "apply": apply}, {"value": 3})
    assert fiber.state is FiberState.PENDING
    assert fiber.get_state() is FiberState.PENDING

    assert await fiber is fiber
    assert fiber.state is FiberState.ACTIVE
    assert fiber.get_state() is FiberState.ACTIVE
    assert seen == [(fiber.context, {"value": 3})]
    assert await fiber is fiber  # awaiting again does not reload


async def test_failed_load_stores_error_and_raises_at_await() -> None:
    ctx = _context()

    def apply(_child_ctx: object, _config: object) -> None:
        raise RuntimeError("boom")

    fiber = ctx.plugin({"name": "broken", "apply": apply}, {})
    with pytest.raises(RuntimeError, match="boom"):
        await fiber

    assert fiber.state is FiberState.FAILED
    assert isinstance(fiber.error, RuntimeError)
    with pytest.raises(RuntimeError, match="boom"):
        await fiber


async def test_effects_are_disposed_in_reverse_order() -> None:
    ctx = _context()
    calls: list[str] = []

    def apply(child_ctx: object, _config: object) -> None:
        child_ctx.effect(lambda: calls.append("inner-first"), label="inner-first")  # type: ignore[attr-defined]
        child_ctx.effect(lambda: calls.append("inner-second"), label="inner-second")  # type: ignore[attr-defined]

    fiber = ctx.plugin({"name": "effects", "apply": apply}, {})
    await fiber
    assert fiber.dispose() is None
    assert calls == ["inner-second", "inner-first"]


async def test_dispose_returns_awaitable_for_async_teardowns() -> None:
    ctx = _context()
    calls: list[str] = []

    async def apply(child_ctx: object, _config: object) -> None:
        async def teardown() -> None:
            await asyncio.sleep(0)
            calls.append("async")

        child_ctx.effect(teardown, label="async")  # type: ignore[attr-defined]

    fiber = ctx.plugin({"name": "async", "apply": apply}, {})
    await fiber
    result = fiber.dispose()
    assert inspect.isawaitable(result)
    await result  # type: ignore[misc]
    assert calls == ["async"]


def test_dispose_without_running_loop_drains_async_teardowns() -> None:
    ctx = _context()
    calls: list[str] = []

    async def apply(child_ctx: object, _config: object) -> None:
        async def teardown() -> None:
            calls.append("drained")

        child_ctx.effect(teardown, label="async")  # type: ignore[attr-defined]

    fiber = ctx.plugin({"name": "sync-drain", "apply": apply}, {})
    asyncio.run(fiber.wait())
    assert fiber.dispose() is None
    assert calls == ["drained"]


async def test_dispose_cascades_children_before_parent_effects() -> None:
    ctx = _context()
    order: list[str] = []

    def inner_apply(child_ctx: object, _config: object) -> None:
        child_ctx.effect(lambda: order.append("inner"), label="inner")  # type: ignore[attr-defined]

    def outer_apply(child_ctx: object, _config: object) -> None:
        child_ctx.plugin({"name": "inner", "apply": inner_apply}, {})  # type: ignore[attr-defined]
        child_ctx.effect(lambda: order.append("outer"), label="outer")  # type: ignore[attr-defined]

    fiber = ctx.plugin({"name": "outer", "apply": outer_apply}, {})
    await fiber
    assert len(fiber.children) == 1
    child = fiber.children[0]
    assert child.state is FiberState.PENDING
    await child
    assert child.state is FiberState.ACTIVE

    fiber.dispose()
    assert order == ["inner", "outer"]
    assert fiber.children == ()


async def test_disposed_fiber_rejects_new_effects() -> None:
    ctx = _context()
    fiber = ctx.plugin({"name": "noop", "apply": lambda _ctx, _config: None}, {})
    await fiber
    fiber.dispose()
    with pytest.raises(RuntimeError, match="disposed fiber"):
        fiber.effect(lambda: None)


def test_root_fiber_is_active() -> None:
    ctx = _context()
    root = ctx.fiber
    assert isinstance(root, Fiber)
    assert root.parent is None
    assert root.state is FiberState.ACTIVE
    assert root.get_state() is FiberState.ACTIVE
    assert root.name == "root"


async def test_inject_dependencies_keep_a_fiber_pending_until_provided() -> None:
    ctx = _context()
    fiber = ctx.plugin(
        {"name": "with-inject", "apply": lambda _ctx, _config: None, "inject": ["tools", "prompt"]}, {}
    )
    assert fiber.inject == ("tools", "prompt")
    await fiber  # missing services do not block the mount
    assert fiber.state is FiberState.PENDING

    ctx.provide("tools", object())
    await asyncio.sleep(0)  # the waiter wakes on the next tick
    assert fiber.state is FiberState.PENDING  # "prompt" is still missing

    ctx.provide("prompt", object())
    await asyncio.sleep(0)
    assert fiber.state is FiberState.ACTIVE


async def test_load_failure_logs_and_subsequent_children_do_not_block() -> None:
    ctx = _context()

    def broken(_child_ctx: object, _config: object) -> None:
        raise ValueError("nope")

    failed = ctx.plugin({"name": "failed", "apply": broken}, {})
    with pytest.raises(ValueError):
        await failed
    healthy = ctx.plugin({"name": "healthy", "apply": lambda _ctx, _config: None}, {})
    await healthy
    assert healthy.state is FiberState.ACTIVE
