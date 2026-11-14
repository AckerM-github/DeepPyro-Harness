"""In-tree log consumer for the runtime hook surface.

Every well-known hook has a real dispatch site somewhere in the runtime; this
plugin is the smallest possible consumer that proves the site fires and makes
the event stream observable in a normal session log. It only observes: emit
handlers return ``None`` and waterfall handlers await the downstream chain and
return its value, so policy is never changed.

High-frequency hooks (``llm/stream`` and ``tool/call``) log at DEBUG; the rest
log at INFO. The message shape is stable (``hook <name> key=value ...``) so
tests and operators can grep it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.hooks import (
    AGENT_ERROR,
    APPROVAL_ASKED,
    APPROVAL_DECIDED,
    APPROVAL_REQUEST,
    LLM_STREAM,
    MODEL_SELECTION,
    SESSION_CREATED,
    SESSION_DISPOSED,
    SYSTEM_PROMPT_ASSEMBLE,
    TOOL_CALL,
)
from deeppyro.hooks.types import HookNext, HookPayload

#: Hooks covered by this plugin, in documentation order.
OBSERVED_HOOKS: tuple[str, ...] = (
    SYSTEM_PROMPT_ASSEMBLE,
    LLM_STREAM,
    MODEL_SELECTION,
    TOOL_CALL,
    SESSION_CREATED,
    SESSION_DISPOSED,
    APPROVAL_REQUEST,
    APPROVAL_ASKED,
    APPROVAL_DECIDED,
    AGENT_ERROR,
)

#: Hooks dispatched through ``waterfall``; their handlers must await next().
WATERFALL_HOOKS = frozenset({SYSTEM_PROMPT_ASSEMBLE, MODEL_SELECTION, APPROVAL_REQUEST})

_DEBUG_HOOKS = frozenset({LLM_STREAM, TOOL_CALL})
_FIELD_ORDER = (
    "sessionId",
    "cwd",
    "preset",
    "provider",
    "model",
    "phase",
    "tool",
    "callId",
    "name",
    "allowed",
    "reason",
    "via",
    "turn",
    "step",
    "policy",
    "frame",
    "sections",
    "request",
)

logger = logging.getLogger("deeppyro.hooks")


def _describe(payload: Mapping[str, Any]) -> str:
    """Render the interesting payload fields as stable ``key=value`` pairs."""
    parts: list[str] = []
    for key in _FIELD_ORDER:
        if key not in payload:
            continue
        value = payload[key]
        if key == "request":
            rendered = repr(getattr(value, "kind", type(value).__name__))
        elif key == "frame":
            rendered = type(value).__name__
        elif key == "sections":
            rendered = str(len(value))
        else:
            rendered = repr(value)
        parts.append(f"{key}={rendered}")
    return " ".join(parts)


class HookObservabilityPlugin(Plugin):
    """Log every dispatched hook; the in-tree reference consumer."""

    plugin_id = "hook-observability"
    version = "0.1.0"
    description = "log each runtime hook dispatch"
    inject: ClassVar[list[str]] = ["hooks"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self._counts: dict[str, int] = {name: 0 for name in OBSERVED_HOOKS}
        self._disposers: list[Callable[[], Any]] = []

    def setup(self, ctx: Any, config: Any) -> None:
        hooks = ctx.service("hooks")
        for name in OBSERVED_HOOKS:
            handler = self._handler(name)
            disposer = ctx.effect(
                lambda h=handler, n=name: hooks.on(n, h),
                f"observe:{name}",
            )
            self._disposers.append(disposer)

    def snapshot(self) -> dict[str, int]:
        """Return how many events of each observed hook have been seen."""
        return dict(self._counts)

    def close(self) -> None:
        """Drop the registrations (idempotent; the kernel does this on unload)."""
        disposers, self._disposers = self._disposers, []
        for disposer in disposers:
            try:
                disposer()
            except Exception:
                logger.exception("hook observability disposer failed")

    def _handler(self, name: str) -> Callable[..., Any]:
        if name in WATERFALL_HOOKS:

            async def observe_waterfall(payload: HookPayload, next_step: HookNext) -> Any:
                self._record(name, payload)
                return await next_step()

            return observe_waterfall

        def observe_emit(payload: HookPayload) -> None:
            self._record(name, payload)

        return observe_emit

    def _record(self, name: str, payload: Mapping[str, Any]) -> None:
        self._counts[name] = self._counts.get(name, 0) + 1
        level = logging.DEBUG if name in _DEBUG_HOOKS else logging.INFO
        logger.log(level, "hook %s %s", name, _describe(payload))


__all__ = ["OBSERVED_HOOKS", "WATERFALL_HOOKS", "HookObservabilityPlugin"]
