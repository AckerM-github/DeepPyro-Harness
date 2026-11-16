"""Unit tests for the native service container and runtime context scopes."""

from __future__ import annotations

import pytest

from deeppyro.api.kernel_types import FiberState
from deeppyro.kernel.context import RuntimeContext
from deeppyro.kernel.fiber import Fiber


def _context() -> RuntimeContext:
    return RuntimeContext.create_root()


def test_provide_get_and_dispose() -> None:
    ctx = _context()
    assert ctx.get("missing") is None
    assert not ctx.has("missing")

    disposer = ctx.provide("greeting", "hello")
    assert ctx.get("greeting") == "hello"
    assert ctx.has("greeting")
    assert ctx.greeting == "hello"

    disposer.dispose()
    assert ctx.get("greeting") is None
    disposer.dispose()  # idempotent


def test_provide_rejects_duplicate_name_in_same_scope() -> None:
    ctx = _context()
    ctx.provide("svc", 1)
    with pytest.raises(RuntimeError, match='service "svc" has been registered at <root>'):
        ctx.provide("svc", 2)


def test_falsy_service_value_is_still_live() -> None:
    ctx = _context()
    ctx.provide("flag", False)
    assert ctx.get("flag") is False
    assert ctx.has("flag")


def test_isolate_shadows_name_for_children_only() -> None:
    root = _context()
    root.provide("shared", "root")
    scoped = root.isolate("shared")
    scoped.provide("shared", "child")

    assert root.get("shared") == "root"
    assert scoped.get("shared") == "child"
    assert scoped.child().get("shared") == "child"
    assert scoped.shared == "child"


def test_isolate_does_not_leak_into_sibling_scope() -> None:
    root = _context()
    root.isolate("only").provide("only", 1)
    assert root.get("only") is None
    assert root.isolate("only").get("only") is None


def test_isolate_full_context_keeps_shared_services_in_scope() -> None:
    root = _context()
    root.provide("shared", "root")
    scoped = root.isolate("other")
    assert scoped.get("shared") == "root"


def test_intercept_layers_merge_nearest_wins() -> None:
    root = _context()
    assert root.resolve_intercept("svc") == {}

    outer = root.intercept("svc", {"a": 1, "b": 1})
    inner = outer.intercept("svc", {"b": 2})
    assert inner.resolve_intercept("svc") == {"a": 1, "b": 2}
    assert outer.resolve_intercept("svc") == {"a": 1, "b": 1}
    assert root.resolve_intercept("svc") == {}


def test_intercept_ignores_other_names() -> None:
    ctx = _context().intercept("one", {"x": 1})
    assert ctx.resolve_intercept("two") == {}


def test_effects_run_in_reverse_order_and_are_idempotent() -> None:
    ctx = _context()
    calls: list[str] = []
    ctx.effect(lambda: calls.append("first"), label="first")
    ctx.effect(lambda: calls.append("second"), label="second")
    ctx.effect(lambda: calls.append("third"), label="third")

    ctx.fiber.dispose()
    assert calls == ["third", "second", "first"]

    ctx.fiber.dispose()
    assert calls == ["third", "second", "first"]


def test_dispose_removes_services_and_hides_them_from_lookups() -> None:
    ctx = _context()
    ctx.provide("value", 1)
    ctx.fiber.dispose()
    assert ctx.get("value") is None
    assert ctx.service_records() == []


def test_failed_provider_hides_service_but_keeps_record() -> None:
    ctx = _context()
    fiber = Fiber(name="broken", parent_ctx=ctx, config={}, runner=lambda _ctx, _config: None)
    fiber._state = FiberState.FAILED
    ctx._container.provide("failing", "value", fiber=fiber)
    assert ctx.get("failing") is None
    assert [record.name for record in ctx.service_records()] == ["failing"]


def test_service_records_snapshot_in_registration_order() -> None:
    ctx = _context()
    ctx.provide("one", 1)
    ctx.provide("two", 2)
    records = ctx.service_records()
    assert [record.name for record in records] == ["one", "two"]
    assert [record.value for record in records] == [1, 2]


def test_service_attribute_access_raises_attribute_error_when_absent() -> None:
    ctx = _context()
    with pytest.raises(AttributeError, match="context has no service 'nope'"):
        _ = ctx.nope


def test_provide_requires_string_name() -> None:
    ctx = _context()
    with pytest.raises(TypeError):
        ctx.provide("", 1)  # type: ignore[arg-type]


def test_child_context_shares_services_and_events() -> None:
    root = _context()
    root.provide("shared", 1)
    child = root.child(fiber=Fiber(name="child", parent_ctx=root, config={}, runner=None))
    assert child.get("shared") == 1
    assert child.root is root
    assert child.parent is root
