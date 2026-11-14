"""Cordis event bridge for runtime hook observations.

The hook service delivers one unified observer stream. This bridge republishes
every dispatch lifecycle as a cordis event so listeners, filters, and the TUI
can follow the hook decision chain without subscribing to each hook:

    hook/invoked   (scope, invoked record)   before the listeners run
    hook/result    (scope, result record)    from the dispatch finally clause

The event this-argument is :class: HookEventScope: it carries the dispatch scope
and implements the cordis filter protocol (__filter__), which is the extension
point where a future listener-level policy can inspect the listener context.
Hot hooks (llm/stream and agent/assistant-stream) are not republished because
they fire once per model frame and would double the event volume.

This module has no kernel imports beyond the hook package, so builtin plugins can
import it without creating a circular dependency.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from deeppyro.hooks.records import HOT_HOOKS, invoked_record, result_record

if TYPE_CHECKING:
    from deeppyro.hooks.service import HookObservation

#: Cordis event emitted when a hook dispatch starts.
EVENT_INVOKED = "hook/invoked"
#: Cordis event emitted when a hook dispatch resolves.
EVENT_RESULT = "hook/result"

__all__ = ["EVENT_INVOKED", "EVENT_RESULT", "HookBridge", "HookEventScope"]


class HookEventScope:
    """Cordis this-argument for bridged hook events.

    scope is the normalized dispatch scope (session/preset) or None for an
    unscoped dispatch. __filter__ is the cordis filter protocol: it runs once
    per listener and currently admits every listener. It is the extension point
    for listener-context policy and may inspect the listener ctx there.
    """

    __slots__ = ("_scope",)

    def __init__(self, scope: Mapping[str, Any] | None = None) -> None:
        self._scope = scope

    @property
    def scope(self) -> Mapping[str, Any] | None:
        """Return the dispatch scope that was handed to the hook listeners."""
        return self._scope

    def __filter__(self, _ctx: Any) -> bool:
        # Filter-protocol extension point: every listener sees bridged events today.
        return True

    def __repr__(self) -> str:
        return f"HookEventScope(scope={self._scope!r})"


class HookBridge:
    """Republish hook observations as cordis events.

    The instance is a callable observer: register it with
    HookService.add_observer; Hot hooks and unknown observation kinds are
    ignored so a malformed observation cannot break the event stream.
    """

    __slots__ = ("_emit",)

    def __init__(self, emit: Callable[..., Any]) -> None:
        if not callable(emit):
            raise TypeError("hook bridge requires a callable emit")
        self._emit = emit

    def __call__(self, observation: HookObservation) -> None:
        """Republish one observation as hook/invoked or hook/result."""
        if observation.hook in HOT_HOOKS:
            return
        kind = observation.kind
        if kind == "invoked":
            event = EVENT_INVOKED
            data = invoked_record(observation)
        elif kind == "resolved":
            event = EVENT_RESULT
            data = result_record(observation)
        else:
            return
        self._emit(HookEventScope(observation.scope), event, data)
