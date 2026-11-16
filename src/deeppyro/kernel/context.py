"""Native runtime context: explicit scope chain over the service container.

A context is an immutable view of a parent context plus at most two scope
layers:

* ``isolate(name)`` gives one service name a fresh scope token, so a child can
  provide the same name without colliding with the parent's provider;
* ``intercept(name, config)`` stacks a config layer used by the provider of
  that name (``resolve_intercept``), nearest layer winning.

There is no ProtoDict, symbol table, tracker or getattr routing: lookups walk
the parent chain for the scope token, then read a plain dict in
:class:`~deeppyro.kernel.container.ServiceContainer`.  Service attribute access
(``ctx.counter``) is kept as a convenience for older plugin code, implemented as
one explicit named lookup rather than the vendor's ``reflect.handler_get``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from deeppyro.api.kernel_types import FiberState
from deeppyro.kernel.container import Disposer, ServiceContainer, ServiceRecord
from deeppyro.kernel.fiber import Fiber

log = logging.getLogger("deeppyro.runtime")

#: Provider fibers in these states make their services visible to lookups.
_VISIBLE_STATES = frozenset({FiberState.ACTIVE, FiberState.LOADING})


class _EventRegistry:
    """Tiny local emitter for non-bus event names (``ctx.on``/``ctx.emit``)."""

    __slots__ = ("_listeners",)

    def __init__(self) -> None:
        self._listeners: dict[str, list[tuple[Callable[..., Any], bool]]] = {}

    def on(self, name: str, handler: Callable[..., Any], *, once: bool = False) -> Disposer:
        if not isinstance(name, str) or not name:
            raise TypeError(f"event name must be a non-empty string, got {name!r}")
        if not callable(handler):
            raise TypeError(f"event handler must be callable, got {type(handler).__name__}")
        listeners = self._listeners.setdefault(name, [])
        entry = (handler, once)
        listeners.append(entry)

        def remove() -> None:
            current = self._listeners.get(name)
            if current is None:
                return
            try:
                current.remove(entry)
            except ValueError:
                return

        return Disposer(remove, label=f"ctx.on({name!r})")

    async def emit(self, name: str, *args: Any) -> None:
        listeners = list(self._listeners.get(name, ()))
        for handler, once in listeners:
            if once:
                current = self._listeners.get(name)
                if current is not None:
                    with contextlib.suppress(ValueError):
                        current.remove((handler, once))
            result = handler(*args)
            if hasattr(result, "__await__"):
                await result


class RuntimeContext:
    """One scope in the plugin host's context tree."""

    __slots__ = (
        "_container",
        "_events",
        "_fiber",
        "_intercept",
        "_isolate",
        "_logger",
        "_parent",
    )

    def __init__(
        self,
        *,
        parent: RuntimeContext | None,
        fiber: Fiber | None,
        container: ServiceContainer,
        isolate: Mapping[str, object] | None = None,
        intercept: Mapping[str, dict[str, Any]] | None = None,
        events: _EventRegistry | None = None,
    ) -> None:
        self._parent = parent
        self._fiber = fiber
        self._container = container
        self._isolate = dict(isolate or {})
        self._intercept = {name: dict(config) for name, config in (intercept or {}).items()}
        self._events = events if events is not None else _EventRegistry()
        self._logger: logging.Logger | None = None

    @classmethod
    def create_root(cls) -> RuntimeContext:
        """Create the root context and its ACTIVE root fiber."""
        container = ServiceContainer()
        ctx = cls(parent=None, fiber=None, container=container)
        ctx._fiber = Fiber.root()
        return ctx

    # -- tree -------------------------------------------------------------

    def child(self, *, fiber: Fiber | None = None) -> RuntimeContext:
        """Derive a context for one plugin fiber (or another scope layer)."""
        return RuntimeContext(
            parent=self,
            fiber=fiber if fiber is not None else self._fiber,
            container=self._container,
            events=self._events,
        )

    def isolate(self, name: str, label: object | None = None) -> RuntimeContext:
        """Shadow one service name with a fresh scope token."""
        if not isinstance(name, str) or not name:
            raise TypeError(f"isolate name must be a non-empty string, got {name!r}")
        token = label if label is not None else object()
        return RuntimeContext(
            parent=self,
            fiber=self._fiber,
            container=self._container,
            isolate={name: token},
            events=self._events,
        )

    def intercept(self, name: str, config: Mapping[str, Any]) -> RuntimeContext:
        """Stack a provider-config layer for one service name."""
        if not isinstance(name, str) or not name:
            raise TypeError(f"intercept name must be a non-empty string, got {name!r}")
        if not isinstance(config, Mapping):
            raise TypeError(f"intercept config for {name!r} must be a mapping")
        return RuntimeContext(
            parent=self,
            fiber=self._fiber,
            container=self._container,
            intercept={name: dict(config)},
            events=self._events,
        )

    def _chain(self) -> list[RuntimeContext]:
        chain: list[RuntimeContext] = []
        current: RuntimeContext | None = self
        while current is not None:
            chain.append(current)
            current = current._parent
        return chain

    @property
    def parent(self) -> RuntimeContext | None:
        return self._parent

    @property
    def root(self) -> RuntimeContext:
        """The topmost context in this tree."""
        return self._chain()[-1]

    @property
    def fiber(self) -> Fiber | None:
        return self._fiber

    @property
    def logger(self) -> logging.Logger:
        if self._logger is None:
            fiber_name = self._fiber.name if self._fiber is not None else "runtime"
            self._logger = logging.getLogger(f"deeppyro.plugin.{fiber_name}")
        return self._logger

    # -- scopes -----------------------------------------------------------

    def resolve_isolate(self, name: str) -> object | None:
        """Return the nearest scope token for a service name, or None."""
        for context in self._chain():
            token = context._isolate.get(name)
            if token is not None:
                return token
        return None

    def resolve_intercept(self, name: str) -> dict[str, Any]:
        """Merge intercept layers for one service name (nearest layer wins)."""
        merged: dict[str, Any] = {}
        for context in reversed(self._chain()):
            config = context._intercept.get(name)
            if config:
                merged.update(config)
        return merged

    # -- services ---------------------------------------------------------

    def provide(self, name: str, value: Any = None, check: Callable[[], Any] | None = None) -> Disposer:
        """Register a service on this context's fiber and return its disposer."""
        record = self._container.provide(
            name,
            value,
            fiber=self._fiber,
            check=check,
            isolate=self.resolve_isolate(name),
        )
        # Plugins waiting for this dependency retry their load once it exists.
        self._container.notify_waiters()
        return self.effect(lambda: self._container.remove(record), label=f"ctx.provide({name!r})")

    def get(self, name: str, default: Any = None) -> Any | None:
        """Return an active service value by name, or ``default``."""
        record = self._container.get(name, self.resolve_isolate(name))
        if record is None:
            return default
        if record.fiber is not None and record.fiber.state not in _VISIBLE_STATES:
            return default
        return record.value

    def has(self, name: str) -> bool:
        """Whether an active service with this name is visible here."""
        return self.get(name) is not None

    def service_records(self) -> list[ServiceRecord]:
        """Snapshot of every registered service (the inspect service's view)."""
        return self._container.records()

    def service_record(self, name: str) -> ServiceRecord | None:
        """Return the registration usable here, or None.

        A provider that is still loading, failed or disposed does not satisfy a
        dependency; only an ACTIVE provider (or a kernel-provided root value,
        which has no plugin fiber) does.
        """
        record = self._container.get(name, self.resolve_isolate(name))
        if record is None:
            return None
        if record.fiber is not None and record.fiber.state is not FiberState.ACTIVE:
            return None
        return record

    def missing_services(self, names: Iterable[str]) -> tuple[str, ...]:
        """Return the inject names this context cannot satisfy yet."""
        return tuple(name for name in names if self.service_record(name) is None)

    # -- dependency waiters ----------------------------------------------

    def register_waiter(self, fiber: Fiber) -> None:
        """Register a pending fiber to retry when a service appears."""
        self._container.add_waiter(fiber)

    def unregister_waiter(self, fiber: Fiber) -> None:
        """Forget a pending fiber (it loaded, failed or was disposed)."""
        self._container.remove_waiter(fiber)

    def notify_waiters(self) -> None:
        """Wake every pending fiber so it can retry loading."""
        self._container.notify_waiters()

    # -- effects and mounting --------------------------------------------

    def effect(self, execute: Callable[[], Any], label: str = "") -> Disposer:
        """Register a teardown callback on this context's fiber."""
        if self._fiber is None:  # pragma: no cover - every context has a fiber
            raise RuntimeError("context has no fiber to own an effect")
        return self._fiber.effect(execute, label=label)

    def plugin(self, plugin: Any, config: Any = None) -> Fiber:
        """Mount a plugin (class, callable or host plugin dict) and return its fiber."""
        from deeppyro.kernel.host import mount

        return mount(self, plugin, config)

    def inject(self, inject: Iterable[str], callback: Callable[..., Any]) -> Fiber:
        """Mount a callback that receives the context (legacy mount helper)."""
        name = getattr(callback, "__name__", "") or "inject"
        return self.plugin({"name": name, "apply": callback, "inject": list(inject)}, None)

    # -- local events -----------------------------------------------------

    def on(
        self,
        event: str,
        handler: Callable[..., Any],
        options: Mapping[str, Any] | None = None,
    ) -> Disposer:
        """Subscribe to a non-bus event name owned by the runtime itself."""
        once = bool((options or {}).get("once", False))
        disposer = self._events.on(event, handler, once=once)
        self.effect(disposer.dispose, label=f"ctx.once({event!r})" if once else f"ctx.on({event!r})")
        return disposer

    def once(
        self, event: str, handler: Callable[..., Any], options: Mapping[str, Any] | None = None
    ) -> Disposer:
        merged = dict(options or {})
        merged["once"] = True
        return self.on(event, handler, merged)

    async def emit(self, *args: Any) -> None:
        """Dispatch a non-bus event to local listeners."""
        if not args:
            raise TypeError("emit requires an event name")
        name = args[0]
        await self._events.emit(str(name), *args[1:])

    # -- attribute compatibility -----------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Resolve ``ctx.<service>`` for older plugins (explicit named lookup)."""
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            container = object.__getattribute__(self, "_container")
        except AttributeError:  # pragma: no cover - construction edge
            raise AttributeError(name) from None
        record = container.get(name, self.resolve_isolate(name))
        if record is not None and (record.fiber is None or record.fiber.state in _VISIBLE_STATES):
            return record.value
        raise AttributeError(f"context has no service {name!r}")

    def __repr__(self) -> str:
        fiber = self._fiber.name if self._fiber is not None else "none"
        return f"<RuntimeContext fiber={fiber!r} services={len(self._container)}>"


__all__ = ["RuntimeContext"]
