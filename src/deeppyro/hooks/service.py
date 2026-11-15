"""The hook facade: the bus, restricted to the names this build still supports.

``EventBus`` (in :mod:`deeppyro.core.bus`) is the transport and knows no names.
This module keeps the historical ``HookService`` surface for the kernel and for
in-tree subscribers, and adds the one piece of policy that belongs to hooks
rather than to the bus: which names may still be registered.

A name retired from the bus (``tool/call``, ``model/selection``,
``approval/asked``, ``approval/decided``) is rejected here with a message that
points at its replacement, so a stale plugin fails loudly instead of silently
observing nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from deeppyro.core.bus import EventBus, HookAggregateError, HookObservation
from deeppyro.hooks.types import DEPRECATED_NAMES, HOOK_NAMES, HookPayload

__all__ = ["HookAggregateError", "HookObservation", "HookService"]


def _unknown_name_message(name: str) -> str:
    known = ", ".join(sorted(HOOK_NAMES))
    return f"unknown hook {name!r}; expected one of: {known}"


class HookService:
    """The kernel bus, reachable through the historical hook API."""

    def __init__(self, bus: EventBus | None = None) -> None:
        """Wrap ``bus`` (a fresh one by default) and keep its registry shared."""
        self._bus = bus if bus is not None else EventBus()

    @property
    def bus(self) -> EventBus:
        """The underlying bus; the kernel wires the same instance to plugins."""
        return self._bus

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
        """Register a handler for a supported hook name and return its disposer."""
        self._validate(name)
        return self._bus.on(name, handler, prepend=prepend, once=once, scope=scope, global_=global_)

    def has(self, name: str) -> bool:
        """Return True when at least one listener is registered for the name."""
        return self._bus.has(name)

    def listeners(self, name: str) -> tuple[Callable[..., Any], ...]:
        """Return every registered handler, ignoring scope filters."""
        return self._bus.listeners(name)

    def add_observer(self, observer: Callable[[HookObservation], None]) -> Callable[[], None]:
        """Observe every dispatch, before and after it runs."""
        return self._bus.add_observer(observer)

    @property
    def observed(self) -> bool:
        """Return True when at least one dispatch observer is registered."""
        return self._bus.observed

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
        self._validate(name)
        return await self._bus.waterfall(name, payload, fallback, scope=scope)

    async def serial(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> list[Any]:
        """Run every matching handler in order and collect their return values."""
        self._validate(name)
        return await self._bus.serial(name, payload, scope=scope)

    async def emit(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        """Run every matching handler in order, logging and isolating handler failures."""
        self._validate(name)
        await self._bus.emit(name, payload, scope=scope)

    async def bail(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> Any:
        """Stop at the first matching handler returning a bailed value."""
        self._validate(name)
        return await self._bus.bail(name, payload, scope=scope)

    async def parallel(
        self,
        name: str,
        payload: HookPayload,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        """Run matching handlers concurrently and aggregate failures."""
        self._validate(name)
        await self._bus.parallel(name, payload, scope=scope)

    # -- policy ------------------------------------------------------------
    def _validate(self, name: str) -> None:
        if name in DEPRECATED_NAMES:
            raise ValueError(
                f"hook {name!r} was retired from the bus; subscribe to what replaces it "
                "(tools/pre-execute, tools/execute, tools/post-execute for tool calls; "
                "llm/stream or agent/request for model selection; approval/request for approvals)"
            )
        if name not in HOOK_NAMES:
            raise ValueError(_unknown_name_message(name))
