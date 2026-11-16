"""Native service container for the DeepPyro plugin host.

Audit of the vendored cordis port (src/deeppyro/_vendor/cordis_port, removed in
this branch): which features DeepPyro actually used, and what replaced them.

* ``ctx.provide`` / ``ctx.get`` / ``ctx.service`` / ``ctx.has`` — the heart of
  the host: 24 provide sites and 26 get sites across builtins (sandbox, slots,
  hooks, tools/params, api/services, inspect, mcp, agents, attachments, goal,
  ...).  The vendor registered through ``reflect.provide`` with a symbol key per
  isolate layer, a ``Tracker`` for ownership and a per-provider ``Impl`` object
  storing dependents for cordis' reactive injection epochs.  Native: one
  :class:`ServiceContainer` keyed by ``(name, isolate token)``; ownership is the
  :class:`~deeppyro.kernel.container.Disposer` registered on the providing fiber.
* ``ctx.reflect`` — only 5 uses: kernel ``App._service_value``, api
  ``PluginContext._lookup``, ``builtin/inspect.services()`` and two helpers.
  None needs a reflection layer: a plain container lookup and a container
  snapshot replace it.  ``ctx.reflect`` is gone.
* ``ctx.isolate(name)`` / ``ctx.intercept(name, config)`` — kernel group rows and
  preset realms scope service names and override provider config.  Native:
  a scope token per isolated name and a config layer per intercepted name on
  the context chain; no ProtoDict prototype chain.
* ``ctx.effect`` — 9 sites (sandbox, hooks, goal, ...) plus every ``provide``
  call: disposal must run in reverse registration order and be idempotent.
  Native: :class:`Disposer` objects owned by the fiber.
* ``ctx.on/once/emit`` — the bus (core.bus.EventBus) carries every domain event;
  no source subscribes to a cordis event name outside api/context.py, so the
  native context keeps only a tiny local emitter for non-bus names.
* ``ctx.plugin`` / ``ctx.inject`` — the kernel, presets and plugin mounting use
  ``ctx.plugin(plugin_dict, config)``; ``ctx.inject`` had zero call sites.
* ``ctx.fiber`` / fiber state — ``builtin/inspect`` compares ``fiber.state`` to
  ``FiberState.ACTIVE``; ``App.row_states`` renders ``fiber.get_state()``; ``App.stop``
  disposes fibers in reverse order (sync call, awaitable result).  The native
  :class:`~deeppyro.kernel.fiber.Fiber` keeps exactly that surface.
* ``ctx.logger`` — plugins ask for a ``logging.Logger`` with printf args;
  PluginContext already builds one per plugin id, so the container does not
  provide a logger service.
* ``_TraceableProxy``, "this" binding, ProtoDict, tracker objects and
  ``_cordis_`` attributes — no production code outside the vendor read them.
  The native runtime has no such machinery.

Design: one container shared by a context tree; contexts are immutable scope
objects with an explicit parent pointer (see kernel/context.py); disposal is a
plain :class:`Disposer` owned by the fiber that registered it.  No symbols, no
prototype chains, no trackers, no getattr routing.
"""

from __future__ import annotations

import inspect
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("deeppyro.kernel")


class Disposer:
    """Idempotent teardown callback.

    A disposer executes its callback at most once.  The callback may be
    synchronous (returns None) or return an awaitable; the awaitable is
    returned to the caller, which is the fiber draining its reverse-ordered
    effect list.  ``dispose()`` is the canonical method; the object is also
    callable so legacy code that treated cordis disposables as callables keeps
    working.
    """

    __slots__ = ("_done", "_execute", "label")

    def __init__(self, execute: Callable[[], Any], label: str = "") -> None:
        if not callable(execute):
            raise TypeError(f"disposer callback must be callable, got {type(execute).__name__}")
        self._execute = execute
        self.label = label
        self._done = False

    @property
    def disposed(self) -> bool:
        """Whether the disposer already ran (or was cancelled)."""
        return self._done

    def dispose(self) -> Any:
        """Run the callback once; return an awaitable when it is asynchronous."""
        if self._done:
            return None
        self._done = True
        result = self._execute()
        return result if inspect.isawaitable(result) else None

    def __call__(self) -> Any:
        return self.dispose()

    def __repr__(self) -> str:
        return f"<Disposer {self.label!r} disposed={self._done}>"


@dataclass(slots=True)
class ServiceRecord:
    """One registered service value.

    Attributes:
        name: Service name ("config", "tools", ...).
        value: The registered object; never None for a live registration.
        fiber: The fiber that provided it (the root fiber for kernel-provided
            services).  A provider that is loading or active makes the service
            visible; a failed or disposed provider does not.
        check: Optional zero-argument availability predicate.  The inspect
            service evaluates it; lookups themselves never run it.
        isolate: Scope token for an isolated registration, or None at root
            scope.
    """

    name: str
    value: Any
    fiber: Any = None
    check: Callable[[], Any] | None = None
    isolate: object | None = None


class ServiceContainer:
    """Name-keyed store of service values for one context tree."""

    __slots__ = ("_lock", "_records")

    def __init__(self) -> None:
        self._records: dict[tuple[str, object | None], ServiceRecord] = {}
        self._lock = threading.RLock()

    def provide(
        self,
        name: str,
        value: Any,
        *,
        fiber: Any = None,
        check: Callable[[], Any] | None = None,
        isolate: object | None = None,
    ) -> ServiceRecord:
        """Register one value, returning its record.

        Raises:
            RuntimeError: another live record already owns the same name in the
                same scope (the second provider is a plugin bug, not a shadow).
        """
        if not isinstance(name, str) or not name:
            raise TypeError(f"service name must be a non-empty string, got {name!r}")
        key = (name, isolate)
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                owner = getattr(existing.fiber, "name", "root")
                raise RuntimeError(f'service "{name}" has been registered at <{owner}>')
            record = ServiceRecord(name=name, value=value, fiber=fiber, check=check, isolate=isolate)
            self._records[key] = record
            return record

    def remove(self, record: ServiceRecord) -> None:
        """Drop one record (idempotent; the providing fiber owns the call)."""
        with self._lock:
            self._records.pop((record.name, record.isolate), None)

    def get(self, name: str, isolate: object | None = None) -> ServiceRecord | None:
        """Return the record for a name in one scope, or None."""
        with self._lock:
            return self._records.get((name, isolate))

    def records(self) -> list[ServiceRecord]:
        """Snapshot every record in registration order."""
        with self._lock:
            return list(self._records.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


__all__ = ["Disposer", "ServiceContainer", "ServiceRecord"]
