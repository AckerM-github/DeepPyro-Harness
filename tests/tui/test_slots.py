"""Tests for the Textual-free slot registry and plugin service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deeppyro.core.errors import DeeppyroError
from deeppyro.tui.slots import (
    CONVERSATION,
    PANEL,
    SETTINGS,
    STATUS,
    SlotError,
    SlotRegistry,
    SlotsPlugin,
    accepts_context,
    apply_conversation_slots,
    build_slot_context,
    call_slot_factory,
    default_slot_id,
    resolve_slot_registry,
)


def test_registry_orders_by_order_then_id() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda context: "late", order=300, id="late")
    registry.add_status_item(lambda context: "early", order=100, id="early")
    registry.add_status_item(lambda context: "middle", order=200, id="middle")
    assert [item.id for item in registry.status_items()] == ["early", "middle", "late"]
    assert [item.id for item in registry.list()] == ["early", "middle", "late"]


def test_registry_filters_by_kind() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda context: "s", id="s")
    registry.add_panel("Panel", lambda context: "p")
    registry.add_settings_page("Settings", lambda context: "g")
    registry.add_conversation_row(lambda event: True, lambda event: "row", id="row")
    assert [item.kind for item in registry.list()] == [CONVERSATION, PANEL, SETTINGS, STATUS]
    assert [item.id for item in registry.panels()] == ["panel"]
    assert [item.id for item in registry.settings_pages()] == ["settings"]
    assert len(registry) == 4
    assert (STATUS, "s") in registry
    assert (STATUS, "missing") not in registry


def test_registry_rejects_duplicate_kind_and_id() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda context: "one", id="clock")
    with pytest.raises(SlotError, match="duplicate status slot id"):
        registry.add_status_item(lambda context: "two", id="clock", plugin_id="other")


def test_registry_same_id_in_different_kinds_is_allowed() -> None:
    registry = SlotRegistry()
    registry.add_status_item(lambda context: "s", id="shared")
    registry.add_panel("Panel", lambda context: "p", id="shared")
    assert registry.get(STATUS, "shared") is not None
    assert registry.get(PANEL, "shared") is not None


def test_registry_dispose_is_idempotent_and_only_removes_own_slot() -> None:
    registry = SlotRegistry()
    dispose_one = registry.add_status_item(lambda context: "one", id="one")
    registry.add_status_item(lambda context: "two", id="two")
    dispose_one()
    dispose_one()
    assert [item.id for item in registry.status_items()] == ["two"]


def test_registry_rejects_bad_factory_and_order() -> None:
    registry = SlotRegistry()
    with pytest.raises(SlotError, match="factory must be callable"):
        registry.add_status_item("not callable")  # type: ignore[arg-type]
    with pytest.raises(SlotError, match="order must be an int"):
        registry.add_status_item(lambda context: "x", order="1")  # type: ignore[arg-type]
    with pytest.raises(SlotError, match="predicate must be callable"):
        registry.add_conversation_row("nope", lambda event: None)  # type: ignore[arg-type]


def test_default_ids_are_deterministic() -> None:
    def render(context: Any) -> str:
        return "x"

    registry = SlotRegistry()
    registry.add_status_item(render)
    first = registry.status_items()[0].id
    assert first.endswith(".render")
    assert first == default_slot_id(STATUS, render)

    other = SlotRegistry()
    other.add_status_item(render, plugin_id="p")
    assert other.status_items()[0].id == first


def test_lambda_ids_include_source_line() -> None:
    registry = SlotRegistry()
    first = registry.add_status_item(lambda ctx: "a")
    second = lambda ctx: "b"
    registry.add_status_item(second)
    ids = [item.id for item in registry.status_items()]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    first()


def test_panel_title_drives_default_id() -> None:
    registry = SlotRegistry()
    registry.add_panel("My Panel!", lambda context: "x")
    registry.add_settings_page("General Settings", lambda context: "y")
    assert registry.panels()[0].id == "my-panel"
    assert registry.settings_pages()[0].id == "general-settings"


def test_dispose_does_not_remove_a_replacement_with_same_id() -> None:
    registry = SlotRegistry()
    dispose = registry.add_status_item(lambda context: "old", id="same")
    dispose()
    registry.add_status_item(lambda context: "new", id="same")
    dispose()
    assert registry.get(STATUS, "same") is not None


def test_accepts_context_and_call_slot_factory() -> None:
    assert accepts_context(lambda context: context) is True
    assert accepts_context(lambda: "x") is False

    def takes_context(context: Any) -> Any:
        return context

    assert call_slot_factory(takes_context, {"a": 1}) == {"a": 1}
    assert call_slot_factory(lambda: "no context", {"a": 1}) == "no context"


class FakeCtx:
    def __init__(self) -> None:
        self.provided: list[tuple[str, Any]] = []

    def provide(self, name: str, value: Any) -> None:
        self.provided.append((name, value))


def test_slots_plugin_provides_shared_registry() -> None:
    ctx = FakeCtx()
    plugin = SlotsPlugin(ctx, {})
    assert plugin.plugin_id == "slots"
    assert plugin.registry is not None
    plugin.setup(ctx, {})
    assert ctx.provided == [("slots", plugin.registry)]
    assert isinstance(plugin.registry, SlotRegistry)


class FakeRuntime:
    cwd = Path("D:/work")
    provider_name = "fake"
    model = "fake-1"
    context_window = 8192
    session_id = "session-1234"
    busy = False

    def session_usage(self) -> dict[str, int]:
        return {"input": 10, "output": 3, "cached": 2}


class FakeApp:
    _phase = "thinking"
    busy = True

    def __init__(self) -> None:
        self.runtime = FakeRuntime()


def test_build_slot_context() -> None:
    context = build_slot_context(FakeApp())
    assert context["cwd"] == str(Path("D:/work"))
    assert context["session_id"] == "session-1234"
    assert context["provider"] == "fake"
    assert context["model"] == "fake-1"
    assert context["phase"] == "thinking"
    assert context["input_tokens"] == 10
    assert context["output_tokens"] == 3
    assert context["cached_tokens"] == 2
    assert context["usage"] == {"input": 10, "output": 3, "cached": 2}
    assert context["context_window"] == 8192
    assert context["busy"] is True


def test_build_slot_context_tolerates_broken_runtime() -> None:
    class Broken:
        def session_usage(self) -> dict[str, int]:
            raise RuntimeError("nope")

    class App:
        runtime = Broken()
        _phase = ""

    context = build_slot_context(App())
    assert context["input_tokens"] == 0
    assert context["phase"] == "idle"
    assert "usage" in context


class FakeConversation:
    def __init__(self) -> None:
        self.widgets: list[Any] = []
        self.errors: list[str] = []

    def mount(self, widget: Any) -> None:
        self.widgets.append(widget)

    def add_error(self, text: str) -> None:
        self.errors.append(text)


def test_apply_conversation_slots_mounts_matching_rows_in_order() -> None:
    registry = SlotRegistry()
    registry.add_conversation_row(
        lambda event: event["type"] == "text",
        lambda event: ("first", event["text"]),
        order=20,
        id="first",
    )
    registry.add_conversation_row(
        lambda event: event["type"] == "text",
        lambda event: ("second", event["text"]),
        order=10,
        id="second",
    )
    registry.add_conversation_row(
        lambda event: event["type"] == "tool_call",
        lambda event: ("tool", event["name"]),
        id="tool",
    )
    conversation = FakeConversation()
    mounted = apply_conversation_slots(conversation, {"type": "text", "text": "hi"}, registry)
    assert [item[0] for item in mounted] == ["second", "first"]
    assert conversation.widgets == mounted
    assert conversation.errors == []


def test_apply_conversation_slots_skips_none_and_isolates_errors() -> None:
    registry = SlotRegistry()
    registry.add_conversation_row(
        lambda event: True,
        lambda event: (_ for _ in ()).throw(RuntimeError("boom")),
        id="broken",
        plugin_id="bad-plugin",
    )
    registry.add_conversation_row(lambda event: True, lambda event: None, id="none")
    registry.add_conversation_row(lambda event: True, lambda event: "ok", id="ok")
    conversation = FakeConversation()
    mounted = apply_conversation_slots(conversation, {"type": "text"}, registry)
    assert mounted == ["ok"]
    assert len(conversation.errors) == 1
    assert "bad-plugin/broken" in conversation.errors[0]
    assert "RuntimeError: boom" in conversation.errors[0]


def test_apply_conversation_slots_reports_mount_failure() -> None:
    class BrokenConversation(FakeConversation):
        def mount(self, widget: Any) -> None:
            raise RuntimeError("cannot mount")

    registry = SlotRegistry()
    registry.add_conversation_row(lambda event: True, lambda event: "widget", id="row")
    conversation = BrokenConversation()
    assert apply_conversation_slots(conversation, {"type": "text"}, registry) == []
    assert "cannot mount" in conversation.errors[0]


def test_apply_conversation_slots_without_registry_is_noop() -> None:
    conversation = FakeConversation()
    assert apply_conversation_slots(conversation, {"type": "text"}, None) == []
    assert conversation.widgets == []


def test_resolve_slot_registry_variants() -> None:
    registry = SlotRegistry()

    class FoundKernel:
        def service(self, name: str) -> Any:
            assert name == "slots"
            return registry

    class MissingKernel:
        def service(self, name: str) -> Any:
            raise KeyError(name)

    assert resolve_slot_registry(registry) is registry
    assert resolve_slot_registry(None) is None
    assert resolve_slot_registry(FoundKernel()) is registry
    assert resolve_slot_registry(MissingKernel()) is None

    class Runtime:
        kernel_app = FoundKernel()

    assert resolve_slot_registry(Runtime()) is registry


def test_slot_error_is_deeppyro_error() -> None:
    assert issubclass(SlotError, DeeppyroError)
