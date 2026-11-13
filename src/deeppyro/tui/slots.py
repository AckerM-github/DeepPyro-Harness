"""Slot registry for plugin-extended terminal UI surfaces.

The TUI is a slot host: plugins contribute small pieces of UI without patching
the application. This module is deliberately Textual-free so it can be imported
by the kernel, tests, and future headless frontends; the widgets that render a
registry live in :mod:`deeppyro.tui.widgets.plugin_slots`.

Four slot kinds are supported:

* `status` - a one-line status item rendered by `PluginStatusBar`;
* `conversation` - an event row rendered into the conversation stream;
* `panel` - a free-form Textual widget shown in `PluginPanelScreen`;
* `settings` - a settings page, rendered by the same panel host.

All registrations return an idempotent dispose callable. Contributions are
de-duplicated by `(kind, id)`; the second registration raises `SlotError`
instead of silently shadowing the first plugin.

The `SlotsPlugin` exposes one shared :class:`SlotRegistry` as the cordis
service `slots`:

    slots = ctx.service("slots")
    dispose = slots.add_status_item(lambda context: f"items: {len(items)}")

Because a bare registry service cannot see the calling fiber, `plugin_id` is
inferred from the callable's `__module__` unless passed explicitly. Plugin
authors adding several items should pass `plugin_id=ctx.plugin_id` and a stable
`id=` to make ownership and ordering explicit.
"""

from __future__ import annotations

import inspect
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal

from deeppyro.api import Plugin
from deeppyro.core.errors import DeeppyroError

STATUS: Final = "status"
CONVERSATION: Final = "conversation"
PANEL: Final = "panel"
SETTINGS: Final = "settings"

SlotKind = Literal["status", "conversation", "panel", "settings"]
SLOT_KINDS: Final[tuple[SlotKind, ...]] = (STATUS, CONVERSATION, PANEL, SETTINGS)
DEFAULT_ORDER: Final = 100


class SlotError(DeeppyroError):
    """A slot registration is invalid or collides with an existing slot."""


@dataclass(frozen=True, slots=True)
class SlotContribution:
    """One plugin contribution to a UI slot.

    Attributes:
        kind: One of `status`, `conversation`, `panel`, `settings`.
        id: Stable identity within the kind; used for de-duplication.
        plugin_id: Plugin that owns the contribution.
        order: Sort key inside a kind (lower renders first).
        factory: Renderer/factory callable. Its signature depends on the kind:
            status takes a context mapping and returns `str`/`rich.text.Text`;
            conversation takes an event dict and returns a Textual widget or
            `None`; panel/settings take a context mapping and return a widget.
        predicate: Optional event filter for conversation rows.
        title: Human-readable title for panel/settings entries.
        metadata: Small plugin-defined payload; never mutated by the host.
    """

    kind: SlotKind
    id: str
    plugin_id: str
    order: int
    factory: Callable[..., Any]
    predicate: Callable[[dict[str, Any]], bool] | None = None
    title: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _is_callable(value: Any) -> bool:
    return callable(value)


def _validate_order(order: int) -> int:
    if not isinstance(order, int) or isinstance(order, bool):
        raise SlotError(f"slot order must be an int, got {type(order).__name__}")
    return order


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return slug or "slot"


def _guess_plugin_id(factory: Any) -> str:
    module = str(getattr(factory, "__module__", "") or "")
    if module in ("", "__main__", "builtins"):
        return ""
    return module


def default_slot_id(kind: str, factory: Any, title: str = "") -> str:
    """Return a deterministic id for a contribution.

    Panels and settings pages prefer their human title; renderers use their
    qualified name. Lambdas include the source line so two lambdas in one
    plugin do not collide accidentally.
    """
    if title and kind in (PANEL, SETTINGS):
        return _slugify(title)
    module = str(getattr(factory, "__module__", "") or "plugin")
    qualname = str(getattr(factory, "__qualname__", "") or getattr(factory, "__name__", "") or "slot")
    if "<lambda>" in qualname:
        code = getattr(factory, "__code__", None)
        line = getattr(code, "co_firstlineno", 0)
        qualname = f"{qualname}:{line}"
    return f"{module}.{qualname}"


def accepts_context(factory: Callable[..., Any]) -> bool:
    """Whether a slot factory wants the host context as its first argument.

    Factories may be written as `lambda context: ...` or as `lambda: ...`;
    both are accepted so simple status items stay terse. Callables whose
    signature cannot be inspected are assumed to take the context.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return True
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            return True
    return False


def call_slot_factory(factory: Callable[..., Any], context: Any) -> Any:
    """Call a slot factory with `context` when it declares a parameter."""
    if accepts_context(factory):
        return factory(context)
    return factory()


class SlotRegistry:
    """Ordered, de-duplicated collection of plugin UI contributions."""

    def __init__(self, *, plugin_id: str = "") -> None:
        self._plugin_id = str(plugin_id or "")
        self._slots: dict[tuple[str, str], SlotContribution] = {}
        self._lock = threading.RLock()

    # -- registration ---------------------------------------------------
    def add_status_item(
        self,
        renderer: Callable[[Mapping[str, Any]], Any],
        order: int = DEFAULT_ORDER,
        *,
        id: str | None = None,
        plugin_id: str = "",
        title: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Callable[[], None]:
        """Register a one-line status renderer; returns a dispose callable."""
        return self._add(
            STATUS,
            factory=renderer,
            order=order,
            slot_id=id,
            plugin_id=plugin_id,
            title=title,
            metadata=metadata,
        )

    def add_conversation_row(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        factory: Callable[[dict[str, Any]], Any],
        order: int = DEFAULT_ORDER,
        *,
        id: str | None = None,
        plugin_id: str = "",
        title: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Callable[[], None]:
        """Register an event row renderer; returns a dispose callable.

        The predicate decides whether the row applies to an event, the factory
        returns a Textual widget (or `None` to skip). Rows are evaluated in
        `(order, id)` order; every matching row is mounted.
        """
        if not _is_callable(predicate):
            raise SlotError("conversation slot predicate must be callable")
        return self._add(
            CONVERSATION,
            factory=factory,
            order=order,
            slot_id=id,
            plugin_id=plugin_id,
            title=title,
            metadata=metadata,
            predicate=predicate,
        )

    def add_panel(
        self,
        title: str,
        factory: Callable[[Mapping[str, Any]], Any],
        order: int = DEFAULT_ORDER,
        *,
        id: str | None = None,
        plugin_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Callable[[], None]:
        """Register a titled panel factory; returns a dispose callable."""
        return self._add(
            PANEL,
            factory=factory,
            order=order,
            slot_id=id or (_slugify(title) if title else None),
            plugin_id=plugin_id,
            title=title,
            metadata=metadata,
        )

    def add_settings_page(
        self,
        title: str,
        factory: Callable[[Mapping[str, Any]], Any],
        order: int = DEFAULT_ORDER,
        *,
        id: str | None = None,
        plugin_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Callable[[], None]:
        """Register a titled settings-page factory; returns a dispose callable."""
        return self._add(
            SETTINGS,
            factory=factory,
            order=order,
            slot_id=id or (_slugify(title) if title else None),
            plugin_id=plugin_id,
            title=title,
            metadata=metadata,
        )

    def _add(
        self,
        kind: SlotKind,
        *,
        factory: Any,
        order: int,
        slot_id: str | None,
        plugin_id: str,
        title: str,
        metadata: Mapping[str, Any] | None,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Callable[[], None]:
        if kind not in SLOT_KINDS:
            raise SlotError(f"unknown slot kind: {kind!r}")
        if not _is_callable(factory):
            raise SlotError(f"{kind} slot factory must be callable")
        validated_order = _validate_order(order)
        owner = str(plugin_id or self._plugin_id or _guess_plugin_id(factory))
        resolved_id = str(slot_id or default_slot_id(kind, factory, title))
        if not resolved_id.strip():
            raise SlotError(f"{kind} slot id must not be empty")
        contribution = SlotContribution(
            kind=kind,
            id=resolved_id,
            plugin_id=owner,
            order=validated_order,
            factory=factory,
            predicate=predicate,
            title=str(title or ""),
            metadata=dict(metadata or {}),
        )
        key = (kind, resolved_id)
        with self._lock:
            existing = self._slots.get(key)
            if existing is not None:
                raise SlotError(
                    f"duplicate {kind} slot id {resolved_id!r}: "
                    f"owned by {existing.plugin_id!r}, also registered by {owner!r}"
                )
            self._slots[key] = contribution

        def dispose() -> None:
            with self._lock:
                current = self._slots.get(key)
                if current is contribution:
                    del self._slots[key]

        return dispose

    # -- queries ---------------------------------------------------------
    def list(self, kind: str | None = None) -> list[SlotContribution]:
        """Contributions sorted by `(kind, order, id)`, optionally by kind."""
        with self._lock:
            values = list(self._slots.values())
        if kind is not None:
            values = [item for item in values if item.kind == kind]
        return sorted(values, key=lambda item: (item.kind, item.order, item.id))

    def status_items(self) -> list[SlotContribution]:
        """Status items in render order."""
        return self.list(STATUS)

    def conversation_rows(self) -> list[SlotContribution]:
        """Conversation rows in evaluation order."""
        return self.list(CONVERSATION)

    def panels(self) -> list[SlotContribution]:
        """Panel factories in display order."""
        return self.list(PANEL)

    def settings_pages(self) -> list[SlotContribution]:
        """Settings-page factories in display order."""
        return self.list(SETTINGS)

    def get(self, kind: str, slot_id: str) -> SlotContribution | None:
        """Return one contribution, or None when it is not registered."""
        with self._lock:
            return self._slots.get((kind, slot_id))

    def clear(self) -> None:
        """Forget every contribution (tests and host shutdown)."""
        with self._lock:
            self._slots.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._slots)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, tuple) and len(key) == 2 and self.get(str(key[0]), str(key[1])) is not None


class SlotsPlugin(Plugin):
    """Kernel plugin exposing the shared :class:`SlotRegistry` as `slots`.

    Add the plugin as a kernel row before any plugin that injects `slots`:

        [[plugins]]
        id = "slots"
        plugin = "deeppyro.tui.slots:SlotsPlugin"

    The registry is provided through `ctx.provide`, so it disappears with the
    plugin fiber automatically. The TUI does not need to construct a registry
    itself.
    """

    plugin_id = "slots"
    version = "0.1.0"
    description = "TUI slot registry: status items, conversation rows, panels, settings"
    inject: ClassVar[list[str]] = []

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.registry = SlotRegistry(plugin_id=self.plugin_id)

    def setup(self, ctx: Any, config: Any) -> None:
        """Provide the registry as a cordis service."""
        ctx.provide("slots", self.registry)


def apply_conversation_slots(
    conversation: Any,
    event: Mapping[str, Any],
    registry: SlotRegistry | None,
    *,
    context: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Mount every conversation row that matches one agent event.

    The host calls this from `DeepPyroApp.handle_event` after (or before) the
    built-in rendering. Each contribution's predicate is evaluated first, then
    its factory; a factory returning a widget has it mounted into the given
    conversation container. Exceptions in one plugin are reported to the
    conversation and never escape, so a broken plugin cannot kill a turn.

    Args:
        conversation: Object with `mount(widget)` and preferably `add_error`.
        event: Agent event mapping (see docs/spec/INTERFACES.md section 7).
        registry: Active slot registry, or None when the plugin is absent.
        context: Optional host context; currently reserved for future use.

    Returns:
        The widgets that were mounted, in evaluation order.
    """
    if registry is None:
        return []
    payload = dict(event or {})
    mounted: list[Any] = []
    for contribution in registry.conversation_rows():
        predicate = contribution.predicate
        try:
            if predicate is not None and not bool(call_slot_factory(predicate, payload)):
                continue
            widget = call_slot_factory(contribution.factory, payload)
        except Exception as exc:  # noqa: BLE001 - one plugin must not break the stream
            _report_slot_error(conversation, contribution, exc)
            continue
        if widget is None:
            continue
        try:
            conversation.mount(widget)
        except Exception as exc:  # noqa: BLE001
            _report_slot_error(conversation, contribution, exc)
            continue
        mounted.append(widget)
    return mounted


def _report_slot_error(conversation: Any, contribution: SlotContribution, exc: BaseException) -> None:
    add_error = getattr(conversation, "add_error", None)
    if not callable(add_error):
        return
    try:
        add_error(
            f"plugin slot {contribution.plugin_id or '?'}/{contribution.id} failed: "
            f"{type(exc).__name__}: {exc}"
        )
    except Exception:  # noqa: BLE001, S110 - error reporting is best effort
        pass


def resolve_slot_registry(source: Any) -> SlotRegistry | None:
    """Return the `slots` service from an app, runtime, or service host.

    Accepts a booted kernel App (`source.service`), a TUI runtime
    (`source.kernel_app.service`), or None. Returns None when the plugin is
    not installed rather than raising, because slots are optional.
    """
    if isinstance(source, SlotRegistry):
        return source
    if source is None:
        return None
    kernel = getattr(source, "kernel_app", None)
    if kernel is None:
        kernel = source
    service = getattr(kernel, "service", None)
    if not callable(service):
        return None
    try:
        registry = service("slots")
    except Exception:  # noqa: BLE001 - optional service, missing is normal
        return None
    return registry if isinstance(registry, SlotRegistry) else None


def build_slot_context(app: Any) -> dict[str, Any]:
    """Build the mapping passed to status/panel renderers.

    Keys are stable and documented in docs/spec/tui-slots.md:
    `app`, `runtime`, `cwd`, `session_id`, `provider`, `model`, `phase`,
    `input_tokens`, `output_tokens`, `cached_tokens`, `context_window`,
    `usage`, `busy`.
    """
    runtime = getattr(app, "runtime", None)
    session_usage = getattr(runtime, "session_usage", None)
    raw_usage: dict[str, Any] = {}
    if callable(session_usage):
        try:
            raw_usage = dict(session_usage())
        except Exception:  # noqa: BLE001 - status context is best effort
            raw_usage = {}
    usage: dict[str, int] = {key: int(raw_usage.get(key, 0) or 0) for key in ("input", "output", "cached")}
    context_window = int(getattr(runtime, "context_window", 0) or 0)
    cwd = getattr(runtime, "cwd", None)
    return {
        "app": app,
        "runtime": runtime,
        "cwd": str(cwd) if cwd is not None else "",
        "session_id": str(getattr(runtime, "session_id", "") or ""),
        "provider": str(getattr(runtime, "provider_name", "") or ""),
        "model": str(getattr(runtime, "model", "") or ""),
        "phase": str(getattr(app, "_phase", "") or "idle"),
        "input_tokens": int(usage.get("input", 0)),
        "output_tokens": int(usage.get("output", 0)),
        "cached_tokens": int(usage.get("cached", 0)),
        "context_window": context_window,
        "usage": usage,
        "busy": bool(getattr(app, "busy", False)),
    }


__all__ = [
    "CONVERSATION",
    "DEFAULT_ORDER",
    "PANEL",
    "SETTINGS",
    "SLOT_KINDS",
    "STATUS",
    "SlotContribution",
    "SlotError",
    "SlotKind",
    "SlotRegistry",
    "SlotsPlugin",
    "apply_conversation_slots",
    "build_slot_context",
    "call_slot_factory",
    "default_slot_id",
    "resolve_slot_registry",
]
