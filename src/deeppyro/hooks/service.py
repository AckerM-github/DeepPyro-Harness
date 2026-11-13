"""Runtime hook service with cordis-style waterfall, serial, and emit dispatch.

The service is designed for one asyncio event loop. Registration and disposal
mutate plain lists, and every dispatch works from a snapshot tuple, so handlers
added or removed while a dispatch is in flight only affect later dispatches.
There is no OS-thread lock: do not call the service from another thread.

Handler kinds
-------------
Waterfall handlers have the signature handler(payload, next). Serial and emit
handlers have the signature handler(payload). Every handler may be synchronous
or asynchronous; an awaitable return value is awaited.

Waterfall semantics
-------------------
Handlers run in registration order (prepend inserts at the front). Each handler
receives the payload and an async next() that continues the chain and returns
its result. A non-None return value short-circuits the chain. Returning None
continues: if the handler already called next() the service returns that
downstream result, otherwise the service calls next() itself. When the chain is
exhausted the fallback is returned; a callable fallback is invoked with no
arguments and an awaitable result is awaited. Calling next() more than once
raises RuntimeError. Handler exceptions propagate to the waterfall caller.

Serial dispatch runs every handler in order and collects the return values in a
list without short-circuiting; exceptions propagate. Emit dispatch also runs
every handler in order, but logs and isolates exceptions so one listener cannot
break the runtime event stream; CancelledError inherits from BaseException and
therefore still propagates to the caller.

Unknown names raise ValueError at registration and at dispatch time. The
read-only helpers has() and listeners() accept unknown names and report an
empty result instead.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from typing import Any

from deeppyro.hooks.types import HOOK_NAMES, HookPayload

logger = logging.getLogger(__name__)

__all__ = ["HookService"]


class HookService:
    """Dispatch hooks to handlers registered by plugins and the runtime."""

    def __init__(self) -> None:
        """Create an empty service with a handler list for every known hook."""
        self._handlers: dict[str, list[Callable[..., Any]]] = {name: [] for name in HOOK_NAMES}

    # -- registration ------------------------------------------------------
    def on(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        prepend: bool = False,
    ) -> Callable[[], None]:
        """Register a handler for a known hook and return an idempotent disposer."""
        handlers = self._registration_list(name)
        if not callable(handler):
            raise TypeError("hook handler must be callable")
        if prepend:
            handlers.insert(0, handler)
        else:
            handlers.append(handler)
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            try:
                handlers.remove(handler)
            except ValueError:
                return

        return dispose

    def has(self, name: str) -> bool:
        """Return True when at least one handler is registered for the name."""
        return bool(self._handlers.get(name))

    def listeners(self, name: str) -> tuple[Callable[..., Any], ...]:
        """Return a snapshot of the handlers registered for the name."""
        return tuple(self._handlers.get(name, ()))

    # -- dispatch ----------------------------------------------------------
    async def waterfall(self, name: str, payload: HookPayload, fallback: Any = None) -> Any:
        """Run handlers in order until one returns a non-None decision."""
        self._validate_name(name)
        handlers = self.listeners(name)
        return await self._waterfall(handlers, 0, payload, fallback, name)

    async def serial(self, name: str, payload: HookPayload) -> list[Any]:
        """Run every handler in order and collect their return values."""
        self._validate_name(name)
        results: list[Any] = []
        for handler in self.listeners(name):
            results.append(await _invoke(handler, payload))
        return results

    async def emit(self, name: str, payload: HookPayload) -> None:
        """Run every handler in order, logging and isolating handler failures."""
        self._validate_name(name)
        for handler in self.listeners(name):
            try:
                await _invoke(handler, payload)
            except Exception:
                # Emit isolates every listener failure by design.
                logger.exception("hook handler for %r failed", name)

    # -- internals ---------------------------------------------------------
    def _registration_list(self, name: str) -> list[Callable[..., Any]]:
        if name not in HOOK_NAMES:
            raise ValueError(_unknown_name_message(name))
        return self._handlers[name]

    def _validate_name(self, name: str) -> None:
        if name not in HOOK_NAMES:
            raise ValueError(_unknown_name_message(name))

    async def _waterfall(
        self,
        handlers: tuple[Callable[..., Any], ...],
        index: int,
        payload: HookPayload,
        fallback: Any,
        name: str,
    ) -> Any:
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
        return await next_step()


async def _invoke(handler: Callable[..., Any], payload: HookPayload) -> Any:
    result = handler(payload)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _resolve_fallback(fallback: Any) -> Any:
    value = fallback() if callable(fallback) else fallback
    if inspect.isawaitable(value):
        value = await value
    return value


def _unknown_name_message(name: str) -> str:
    known = ", ".join(sorted(HOOK_NAMES))
    return f"unknown hook {name!r}; expected one of: {known}"
