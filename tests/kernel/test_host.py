"""Unit tests for mounting plugins on the native host."""

from __future__ import annotations

import pytest

from deeppyro.api.kernel_types import FiberState
from deeppyro.kernel.context import RuntimeContext
from deeppyro.kernel.host import mount


def _context() -> RuntimeContext:
    return RuntimeContext.create_root()


async def test_dict_plugin_receives_child_context_and_config() -> None:
    ctx = _context()

    async def apply(child_ctx: object, config: object) -> None:
        child_ctx.provide("demo", config)  # type: ignore[attr-defined]

    fiber = mount(ctx, {"name": "demo", "apply": apply}, {"n": 1})
    await fiber
    assert fiber.state is FiberState.ACTIVE
    assert ctx.get("demo") == {"n": 1}
    assert fiber.context is not ctx
    assert fiber.context.parent is ctx
    assert fiber.context.fiber is fiber


async def test_dict_plugin_returns_teardown_callable() -> None:
    ctx = _context()
    calls: list[str] = []

    async def apply(_child_ctx: object, _config: object) -> object:
        def teardown() -> None:
            calls.append("teardown")

        return teardown

    fiber = mount(ctx, {"name": "with-teardown", "apply": apply}, {})
    await fiber
    assert fiber.dispose() is None
    assert calls == ["teardown"]
    fiber.dispose()
    assert calls == ["teardown"]


async def test_legacy_class_plugin_runs_init_and_stop() -> None:
    ctx = _context()
    calls: list[str] = []

    class Legacy:
        plugin_id = "legacy"

        def __init__(self, child_ctx: object, config: object) -> None:
            self.ctx = child_ctx
            self.config = config

        def init(self) -> None:
            calls.append("init")

        def stop(self) -> None:
            calls.append("stop")

    fiber = mount(ctx, Legacy, {"value": 1})
    assert fiber.name == "legacy"
    assert fiber.inject == ()
    await fiber
    assert calls == ["init"]
    fiber.dispose()
    assert calls == ["init", "stop"]


async def test_async_legacy_stop_is_drained() -> None:
    ctx = _context()
    calls: list[str] = []
    import asyncio

    class Legacy:
        plugin_id = "legacy-async"

        def __init__(self, child_ctx: object, config: object) -> None:
            pass

        async def stop(self) -> None:
            await asyncio.sleep(0)
            calls.append("stop")

    fiber = mount(ctx, Legacy, {})
    await fiber
    result = fiber.dispose()
    assert result is not None
    assert hasattr(result, "__await__")
    await result
    assert calls == ["stop"]


async def test_callable_plugin_is_applied_with_config() -> None:
    ctx = _context()
    calls: list[object] = []

    def plugin(child_ctx: object, config: object) -> None:
        calls.append((child_ctx, config))

    fiber = mount(ctx, plugin, {"x": 2})
    assert fiber.name == "plugin"
    await fiber
    assert calls == [(fiber.context, {"x": 2})]


async def test_nested_plugins_mount_under_parent_fiber() -> None:
    ctx = _context()

    def child_apply(_child_ctx: object, _config: object) -> None:
        return None

    async def parent_apply(child_ctx: object, _config: object) -> None:
        child_ctx.plugin({"name": "child", "apply": child_apply}, {})  # type: ignore[attr-defined]

    parent = mount(ctx, {"name": "parent", "apply": parent_apply}, {})
    await parent
    assert len(parent.children) == 1
    assert parent.children[0].name == "child"
    assert parent.children[0].state is FiberState.PENDING

    await parent.children[0]
    assert parent.children[0].state is FiberState.ACTIVE

    parent.dispose()
    assert parent.state is FiberState.DISPOSED
    assert parent.children == ()


async def test_unsupported_plugin_object_raises() -> None:
    ctx = _context()
    with pytest.raises(TypeError, match="unsupported plugin object"):
        mount(ctx, object(), {})


async def test_plugin_dict_without_apply_raises() -> None:
    ctx = _context()
    with pytest.raises(TypeError, match="has no callable apply"):
        mount(ctx, {"name": "bad"}, {})


async def test_apply_object_with_apply_method() -> None:
    ctx = _context()
    calls: list[str] = []

    class ApplyObject:
        plugin_id = "apply-object"

        def apply(self, child_ctx: object, config: object) -> None:
            calls.append("apply")

    fiber = mount(ctx, ApplyObject(), {})
    assert fiber.name == "apply-object"
    await fiber
    assert calls == ["apply"]
