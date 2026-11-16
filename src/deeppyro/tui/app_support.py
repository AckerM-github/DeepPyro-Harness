"""Module-level constants and helpers shared by the layered TUI app modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
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

#: Session record types that bracket one hook invocation.
_HOOK_INVOKED_RECORD = "hook/invoked"
_HOOK_RESULT_RECORD = "hook/result"


def _record_field(record: Any, name: str) -> Any:
    """Read a session record field from a mapping or an object."""
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


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


def _normalize_hook_rows(rows: Any) -> list[dict[str, Any]]:
    """Keep the mapping rows of a SessionQuery.hook_trace result."""
    if not isinstance(rows, (list, tuple)):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _hook_rows_from_records(session: Any) -> list[dict[str, Any]]:
    """Pair hook records from raw session records, newest last."""
    records = getattr(session, "records", None)
    if not isinstance(records, (list, tuple)):
        return []
    pending: list[dict[str, Any]] = []
    for record in records:
        record_type = _record_field(record, "type")
        data = _record_field(record, "data")
        if not isinstance(data, Mapping):
            continue
        hook = str(data.get("hook") or "")
        mode = str(data.get("mode") or "")
        if record_type == _HOOK_INVOKED_RECORD:
            pending.append({"hook": hook, "mode": mode, "invoked": dict(data), "result": None})
        elif record_type == _HOOK_RESULT_RECORD:
            for row in reversed(pending):
                if row["result"] is None and row["hook"] == hook:
                    row["result"] = dict(data)
                    break
    return pending[-HOOK_TRACE_LIMIT:]
