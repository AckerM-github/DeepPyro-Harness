"""Lifecycle object for one mounted plugin.

Native replacement for the vendored cordis ``Fiber`` (see
:mod:`deeppyro.kernel.container` for the audit of the port).  Kept semantics,
all of which are observable through ``api.FiberState``, ``App.row_states``,
``builtin/inspect`` and the preset realm teardown:

* a fiber starts PENDING, moves to LOADING while its plugin applies, then
  ACTIVE; a failed apply moves it to FAILED with the exception stored, and a
  disposed fiber is DISPOSED forever;
* ``fiber.state`` and ``fiber.get_state()`` agree and use
  :class:`deeppyro.api.FiberState`;
* awaiting a fiber waits for its load and re-raises the load failure, so the
  kernel surfaces boot errors at the mount point;
* ``fiber.effect(execute, label)`` registers a disposal callback; disposal runs
  effects in reverse registration order and is idempotent, awaiting async
  teardowns;
* ``fiber.dispose()`` is a synchronous call that also disposes child fibers
  (plugins mounted from this plugin's context) in reverse creation order, and
  returns None or an awaitable (the caller awaits it in a running loop; with no
  loop the fiber drains async teardowns itself).

``plugin.inject`` names behave like cordis dependencies: a fiber loads as soon
as its inject services are active.  When one is missing at mount time the fiber
stays PENDING (boot is not blocked, so a disabled provider leaves its consumers
pending instead of aborting startup) and a dependency waiter retries the load
when a provider appears.  The inject names themselves are still advisory
ordering metadata: a plugin that resolves its dependencies lazily can be
mounted before them and simply start later.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Iterable
from typing import Any

from deeppyro.api.kernel_types import FiberState
from deeppyro.kernel.container import Disposer

log = logging.getLogger("deeppyro.kernel")

#: Callable applied inside the fiber: (context, config) -> teardown callable or
#: awaitable yielding one.  A root fiber has no runner.
PluginRunner = Callable[[Any, Any], Any]


class Fiber:
    """One mounted plugin instance (or the root context) in the plugin tree."""

    __slots__ = (
        "_children",
        "_config",
        "_context",
        "_disposables",
        "_error",
        "_inject",
        "_loaded",
        "_lock",
        "_name",
        "_parent",
        "_parent_ctx",
        "_runner",
        "_state",
    )

    def __init__(
        self,
        *,
        name: str = "",
        parent_ctx: Any = None,
        parent: Fiber | None = None,
        config: Any = None,
        inject: Iterable[str] = (),
        runner: PluginRunner | None = None,
    ) -> None:
        self._name = str(name or "")
        self._config = {} if config is None else config
        self._inject = tuple(inject)
        self._runner = runner
        self._parent = parent
        self._parent_ctx = parent_ctx
        self._context: Any = None
        self._children: list[Fiber] = []
        self._disposables: list[Disposer] = []
        self._state = FiberState.PENDING
        self._error: BaseException | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    @classmethod
    def root(cls) -> Fiber:
        """Create the tree's root fiber (always ACTIVE; owns kernel effects)."""
        fiber = cls(name="root")
        fiber._state = FiberState.ACTIVE
        fiber._loaded = True
        return fiber

    # -- introspection ----------------------------------------------------

    @property
    def name(self) -> str:
        return self._name or "root"

    @property
    def state(self) -> FiberState:
        return self._state

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def inject(self) -> tuple[str, ...]:
        return self._inject

    @property
    def context(self) -> Any:
        """The context this plugin applies on (None until load starts)."""
        return self._context

    @property
    def parent(self) -> Fiber | None:
        return self._parent

    @property
    def children(self) -> tuple[Fiber, ...]:
        return tuple(self._children)

    def get_state(self) -> FiberState:
        """Return the current lifecycle state (public, used by inspect/app)."""
        return self._state

    # -- effects ----------------------------------------------------------

    def effect(self, execute: Callable[[], Any], label: str = "") -> Disposer:
        """Register a teardown callback; later registrations dispose first."""
        if self._state is FiberState.DISPOSED:
            raise RuntimeError(f"cannot register an effect on disposed fiber <{self.name}>")
        disposer = Disposer(execute, label=str(label))
        self._disposables.append(disposer)
        return disposer

    # -- load -------------------------------------------------------------

    async def wait(self) -> Fiber:
        """Load the plugin (once) and return this fiber; re-raise load errors.

        A plugin whose required inject services are missing stays PENDING: the
        mount succeeds (a disabled provider does not block boot) and the fiber
        is woken when a provider appears.
        """
        if self._state is FiberState.FAILED and self._error is not None:
            raise self._error
        if self._state is FiberState.DISPOSED:
            raise RuntimeError(f"plugin fiber <{self.name}> has been disposed")
        if self._loaded:
            return self
        async with self._lock:
            if self._loaded:
                return self
            if self._state is FiberState.FAILED and self._error is not None:
                raise self._error
            if not self._ready():
                self._register_waiter()
                return self
            await self._load()
        if self._state is FiberState.FAILED and self._error is not None:
            raise self._error
        return self

    def __await__(self) -> Any:
        return self.wait().__await__()

    def _ready(self) -> bool:
        """Whether every inject dependency has an active provider."""
        if self._parent_ctx is None or not self._inject:
            return True
        return not self._parent_ctx.missing_services(self._inject)

    def _register_waiter(self) -> None:
        if self._parent_ctx is not None:
            self._parent_ctx.register_waiter(self)

    def _unregister_waiter(self) -> None:
        if self._parent_ctx is not None:
            self._parent_ctx.unregister_waiter(self)

    async def resume(self) -> None:
        """Retry a PENDING load after one of its dependencies was provided.

        Background activation failures mark the fiber FAILED and are logged;
        they never propagate into the boot loop, which already moved on.
        """
        if self._state is not FiberState.PENDING:
            return
        async with self._lock:
            if self._state is not FiberState.PENDING:
                return
            if not self._ready():
                return
            try:
                await self._load()
            except Exception:
                log.exception("plugin %s failed to load after its dependencies appeared", self.name)

    async def _load(self) -> None:
        self._state = FiberState.LOADING
        try:
            context = self._parent_ctx.child(fiber=self) if self._parent_ctx is not None else None
            self._context = context
            result = None
            if self._runner is not None:
                result = self._runner(context, self._config)
                if inspect.isawaitable(result):
                    result = await result
            if callable(result):
                self.effect(result, label=f"{self.name}: dispose")
            self._state = FiberState.ACTIVE
            self._loaded = True
        except asyncio.CancelledError:
            self._state = FiberState.FAILED
            self._unregister_waiter()
            raise
        except Exception as exc:
            self._error = exc
            self._state = FiberState.FAILED
            self._unregister_waiter()
            log.debug("plugin fiber %s failed to load: %s", self.name, exc)
            raise
        self._unregister_waiter()
        if self._parent_ctx is not None:
            self._parent_ctx.notify_waiters()

    # -- disposal ---------------------------------------------------------

    def dispose(self) -> Any:
        """Dispose children and effects in reverse order; idempotent.

        Returns None when everything was synchronous, or an awaitable that
        drains asynchronous teardowns.  Without a running event loop the fiber
        drains those teardowns itself so fire-and-forget callers (the preset
        realm teardown) never leak un-awaited coroutines.
        """
        if self._state is FiberState.DISPOSED:
            return None
        self._state = FiberState.DISPOSED
        self._unregister_waiter()
        pending: list[Any] = []
        for child in reversed(self._children):
            try:
                result = child.dispose()
            except Exception:
                log.exception("plugin %s child dispose failed", self.name)
                continue
            if inspect.isawaitable(result):
                pending.append(result)
        self._children.clear()
        for disposer in reversed(self._disposables):
            try:
                result = disposer.dispose()
            except Exception:
                log.exception("plugin %s dispose failed", self.name)
                continue
            if inspect.isawaitable(result):
                pending.append(result)
        self._disposables.clear()
        self._loaded = False
        if not pending:
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            for item in pending:
                try:
                    asyncio.run(_drain(item))
                except Exception:
                    log.exception("plugin %s async dispose failed", self.name)
            return None
        return _drain_all(pending, self.name)


async def _drain(item: Any) -> None:
    await item


async def _drain_all(pending: list[Any], name: str) -> None:
    for item in pending:
        try:
            await item
        except Exception:
            log.exception("plugin %s async dispose failed", name)


__all__ = ["Fiber", "PluginRunner"]
