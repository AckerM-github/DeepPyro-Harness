"""The kernel event bus: one registry, five dispatch modes, no domain names.

DSH dispatches every domain event through a single cordis ``EventsService``. This
module is the transport half of that shape: it knows how to register listeners,
filter them by scope, freeze payloads, notify observers, and run the five cordis
modes (emit, serial, waterfall, bail, parallel). It deliberately knows *nothing*
about which names exist -- that is :mod:`deeppyro.core.events` -- and nothing
about which names are still allowed on the hook facade -- that is
:class:`deeppyro.hooks.service.HookService`.

Splitting transport from catalogue is what lets the kernel hand the same registry
to plugins (``ctx.on``), to the hook facade, and to the journal, instead of
keeping a second private listener table.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from deeppyro.hooks.types import HookPayload

logger = logging.getLogger(__name__)

__all__ = ["EventBus", "HookAggregateError", "HookObservation"]


@dataclass(frozen=True, slots=True)
class _Listener:
    """One registered handler plus the scope metadata used for filtering."""

    handler: Callable[..., Any]
    scope: Mapping[str, Any] | None
    global_: bool


@dataclass(frozen=True, slots=True)
class HookObservation:
    """One dispatch lifecycle event delivered to observers.

    kind is "invoked" before the mode runs or "resolved" from the dispatch
    finally clause. payload is the frozen mapping the listeners received,
    scope is the normalized dispatch scope, and result/error describe the
    outcome (result is None for emit and parallel).
    """

    kind: str
    hook: str
    mode: str
    scope: Mapping[str, Any] | None
    payload: Mapping[str, Any]
    result: Any = None
    error: BaseException | None = None
    duration_ms: float = 0.0


class HookAggregateError(Exception):
    """Raised by parallel() when one or more listeners fail."""

    def __init__(self, errors: tuple[BaseException, ...], hook: str | None = None) -> None:
        self.errors = errors
        self.hook = hook
        where = f" for hook {hook!r}" if hook is not None else ""
        super().__init__(f"{len(errors)} hook listener(s) failed{where}")


class EventBus:
    """Dispatch domain events to listeners registered by anyone."""

    def __init__(self) -> None:
        """Create an empty service with a listener list for every known hook."""
        self._listeners: dict[str, list[_Listener]] = {}
        self._observers: list[Callable[[HookObservation], None]] = []

    # -- registration ------------------------------------------------------
    def on(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        prepend: bool = False,
        once: bool = False,
        scope: Mapping[str, Any] | None = None,
        global_: bool = False,
    ) -> Callable[[], None]:
        """Register a handler for a known hook and return an idempotent disposer.

        scope restricts the listener to dispatches whose own scope contains every
        pair in the listener scope. An empty mapping is normalized to None (no
        restriction). global_=True bypasses filtering and receives every dispatch.
        Scoped listeners do not run for a dispatch without a scope.

        With once=True the registry entry disposes itself immediately before the
        wrapped handler runs, so the listener fires at most once even though each
        dispatch works from a snapshot tuple. The returned disposer stays
        idempotent.
        """
        listeners = self._registration_list(name)
        if not callable(handler):
            raise TypeError("hook handler must be callable")
        normalized = _normalize_scope(scope)
        entry = _Listener(handler=handler, scope=normalized, global_=bool(global_))
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            try:
                listeners.remove(entry)
            except ValueError:
                return

        if once:

            def once_handler(*args: Any, **kwargs: Any) -> Any:
                dispose()
                return handler(*args, **kwargs)

            entry = _Listener(handler=once_handler, scope=normalized, global_=entry.global_)

        if prepend:
            listeners.insert(0, entry)
        else:
            listeners.append(entry)
        return dispose

    def has(self, name: str) -> bool:
        """Return True when at least one listener is registered for the name."""
        return bool(self._listeners.get(name))

    def listeners(self, name: str) -> tuple[Callable[..., Any], ...]:
        """Return all registered handlers for the name, ignoring scope filters."""
        return tuple(entry.handler for entry in self._listeners.get(name, ()))

    def add_observer(self, observer: Callable[[HookObservation], None]) -> Callable[[], None]:
        """Register a dispatch observer and return an idempotent disposer.

        Every dispatch delivers exactly one invoked observation before its
        listeners run and one resolved observation from its finally clause.
        Observer failures are logged with logger.exception and cannot change
        the dispatch result. An observer added or disposed while a dispatch is
        running only affects later notifications.
        """
        if not callable(observer):
            raise TypeError("hook observer must be callable")
        self._observers.append(observer)
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            for index, current in enumerate(self._observers):
                if current is observer:
                    del self._observers[index]
                    return

        return dispose

    @property
    def observed(self) -> bool:
        """Return True when at least one dispatch observer is registered.

        Dispatch call sites that skip work when no listener is registered should
        still dispatch while observed is True: the journal and the cordis bridge
        can only record fallback and veto decisions when the hook runs at all.
        Hot hooks stay listener-gated because they fire once per model frame.
        """
        return bool(self._observers)

    # -- dispatch ----------------------------------------------------------
    async def waterfall(
        self,
        name: str,
        payload: HookPayload,
        fallback: Any = None,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> Any:
        """Run the waterfall chain; a handler that returns None without next() vetoes it."""
        handlers = self._dispatch_handlers(name, scope)
        frozen = _freeze(payload)
        frozen_scope = _normalize_scope(scope)
        started = self._begin(name, "waterfall", frozen, frozen_scope)
        result: Any = None
        error: BaseException | None = None
        try:
            result = await self._waterfall(handlers, 0, frozen, fallback, name)
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._end(name, "waterfall", frozen, frozen_scope, result, error, started)

    async def serial(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> list[Any]:
        """Run every matching handler in order and collect their return values."""
        handlers = self._dispatch_handlers(name, scope)
        frozen = _freeze(payload)
        frozen_scope = _normalize_scope(scope)
        started = self._begin(name, "serial", frozen, frozen_scope)
        results: list[Any] = []
        error: BaseException | None = None
        try:
            for handler in handlers:
                if _cancelled(frozen):
                    break
                results.append(await _invoke(handler, frozen))
            return results
        except BaseException as exc:
            error = exc
            raise
        finally:
            result = None if error is not None else results
            self._end(name, "serial", frozen, frozen_scope, result, error, started)

    async def emit(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        """Run every matching handler in order, logging and isolating handler failures."""
        handlers = self._dispatch_handlers(name, scope)
        frozen = _freeze(payload)
        frozen_scope = _normalize_scope(scope)
        started = self._begin(name, "emit", frozen, frozen_scope)
        error: BaseException | None = None
        try:
            for handler in handlers:
                if _cancelled(frozen):
                    break
                try:
                    await _invoke(handler, frozen)
                except Exception:
                    # Emit isolates every listener failure by design.
                    logger.exception("hook handler for %r failed", name)
            return
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._end(name, "emit", frozen, frozen_scope, None, error, started)

    async def bail(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> Any:
        """Stop at the first matching handler returning a bailed value (not None/False)."""
        handlers = self._dispatch_handlers(name, scope)
        frozen = _freeze(payload)
        frozen_scope = _normalize_scope(scope)
        started = self._begin(name, "bail", frozen, frozen_scope)
        result: Any = None
        error: BaseException | None = None
        try:
            for handler in handlers:
                if _cancelled(frozen):
                    return None
                result = await _invoke(handler, frozen)
                if _is_bailed(result):
                    return result
            return None
        except BaseException as exc:
            error = exc
            raise
        finally:
            resolved = None if error is not None else result
            self._end(name, "bail", frozen, frozen_scope, resolved, error, started)

    async def parallel(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        """Run matching handlers concurrently and aggregate failures into HookAggregateError."""
        handlers = self._dispatch_handlers(name, scope)
        frozen = _freeze(payload)
        frozen_scope = _normalize_scope(scope)
        started = self._begin(name, "parallel", frozen, frozen_scope)
        error: BaseException | None = None
        try:
            if _cancelled(frozen):
                return
            results = await asyncio.gather(
                *(_invoke(handler, frozen) for handler in handlers),
                return_exceptions=True,
            )
            errors = tuple(result for result in results if isinstance(result, BaseException))
            if errors:
                raise HookAggregateError(errors, name) from errors[0]
            return
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._end(name, "parallel", frozen, frozen_scope, None, error, started)

    # -- internals ---------------------------------------------------------
    def _begin(
        self,
        name: str,
        mode: str,
        payload: Mapping[str, Any],
        scope: Mapping[str, Any] | None,
    ) -> float:
        """Notify observers that a dispatch is starting; return its start time."""
        self._notify(
            HookObservation(
                kind="invoked",
                hook=name,
                mode=mode,
                scope=scope,
                payload=payload,
            )
        )
        return time.monotonic()

    def _end(
        self,
        name: str,
        mode: str,
        payload: Mapping[str, Any],
        scope: Mapping[str, Any] | None,
        result: Any,
        error: BaseException | None,
        started: float,
    ) -> None:
        """Notify observers that a dispatch finished, including its duration."""
        self._notify(
            HookObservation(
                kind="resolved",
                hook=name,
                mode=mode,
                scope=scope,
                payload=payload,
                result=result,
                error=error,
                duration_ms=(time.monotonic() - started) * 1000.0,
            )
        )

    def _notify(self, observation: HookObservation) -> None:
        """Deliver one observation, isolating observer failures from dispatch."""
        for observer in tuple(self._observers):
            try:
                observer(observation)
            except Exception:
                logger.exception("hook observer for %r failed", observation.hook)

    def _registration_list(self, name: str) -> list[_Listener]:
        return self._listeners.setdefault(name, [])

    def _dispatch_handlers(
        self,
        name: str,
        scope: Mapping[str, Any] | None,
    ) -> tuple[Callable[..., Any], ...]:
        wanted = _normalize_scope(scope)
        entries = self._listeners.get(name, ())
        if wanted is None:
            return tuple(entry.handler for entry in entries if entry.global_ or entry.scope is None)
        return tuple(
            entry.handler
            for entry in entries
            if entry.global_ or entry.scope is None or _scope_matches(entry.scope, wanted)
        )

    async def _waterfall(
        self,
        handlers: tuple[Callable[..., Any], ...],
        index: int,
        payload: Mapping[str, Any],
        fallback: Any,
        name: str,
    ) -> Any:
        if _cancelled(payload):
            return await _resolve_fallback(fallback)
        if index >= len(handlers):
            return await _resolve_fallback(fallback)
        handler = handlers[index]
        called = False
        next_result: Any = None

        async def next_step() -> Any:
            nonlocal called, next_result
            if called:
                raise RuntimeError(f"next() called more than once for hook {name!r}")
            called = True
            next_result = await self._waterfall(handlers, index + 1, payload, fallback, name)
            return next_result

        result = handler(payload, next_step)
        if inspect.isawaitable(result):
            result = await result
        if result is not None:
            return result
        if called:
            return next_result
        return None


def _normalize_scope(scope: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Validate and copy a scope mapping; an empty mapping means unscoped."""
    if scope is None:
        return None
    if not isinstance(scope, Mapping):
        raise TypeError("hook scope must be a mapping of string keys to scalar values")
    if not scope:
        return None
    for key, value in scope.items():
        if not isinstance(key, str):
            raise TypeError("hook scope keys must be strings")
        if isinstance(value, bool):
            continue
        if not isinstance(value, (str, int)):
            raise TypeError("hook scope values must be strings, integers, or booleans")
    return MappingProxyType(dict(scope))


def _scope_matches(
    listener_scope: Mapping[str, Any],
    dispatch_scope: Mapping[str, Any],
) -> bool:
    """Return True when the listener scope is a subset of the dispatch scope."""
    return all(
        key in dispatch_scope and dispatch_scope[key] == value for key, value in listener_scope.items()
    )


async def _invoke(handler: Callable[..., Any], payload: Mapping[str, Any]) -> Any:
    result = handler(payload)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _resolve_fallback(fallback: Any) -> Any:
    value = fallback() if callable(fallback) else fallback
    if inspect.isawaitable(value):
        value = await value
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _is_bailed(value: Any) -> bool:
    return value is not None and value is not False


def _cancelled(payload: Mapping[str, Any]) -> bool:
    signal = payload.get("signal")
    is_set = getattr(signal, "is_set", None)
    return callable(is_set) and bool(is_set())
