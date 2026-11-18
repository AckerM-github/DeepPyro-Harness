"""Module-level constants and helpers shared by the layered TUI app modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from deeppyro.tui.session_view import goal_lines
from deeppyro.tui.widgets.conversation import ConversationView

if TYPE_CHECKING:
    from deeppyro.tui.app import DeepPyroApp


RuntimeFactory = Callable[["DeepPyroApp"], Awaitable[Any]]

#: Sentinel returned by the /cwd picker when the user wants a free-text path.
CUSTOM_WORKSPACE = "\x00custom-workspace"
CUSTOM_WORKSPACE_LABEL = "Enter path..."

#: How many paired hook decisions the /hooks viewer reads and shows.
HOOK_TRACE_LIMIT = 50


def _goal_service(runtime: Any) -> Any | None:
    """Resolve the optional goal service from the booted runtime."""
    if runtime is None:
        return None
    direct = getattr(runtime, "goal", None)
    if direct is not None:
        return direct
    resolver = getattr(runtime, "_service", None)
    if callable(resolver):
        try:
            service = resolver("goal")
        except Exception:  # noqa: BLE001 - the service is optional
            service = None
        if service is not None:
            return service
    kernel_app = getattr(runtime, "kernel_app", None)
    resolve = getattr(kernel_app, "service", None)
    if not callable(resolve):
        return None
    try:
        return resolve("goal")
    except Exception:  # noqa: BLE001 - the service is optional
        return None


def _emit_goal(conversation: ConversationView, view: Any) -> None:
    """Write the fixed `/goal` block into the conversation."""
    for line in goal_lines(view):
        conversation.add_info(line)
