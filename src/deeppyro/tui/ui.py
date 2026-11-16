"""Textual-free UI action service for plugins.

Plugins never touch Textual widgets from their own threads. They call
:class:`UiService` (the kernel service named ``ui``), which enqueues small,
immutable :class:`UiAction` values under a lock. The TUI app binds itself as
the backend and drains the queue from a ``set_interval(0.1)`` timer on the
Textual thread, so widget access always happens on the UI thread.

The module deliberately imports no Textual: headless frontends can load the
service, and every action is counted in a no-op sink when no backend is bound.

    ui = ctx.ui
    ui.notify("index rebuilt", level="info")
    ui.set_status("indexer", "12 docs")
    ui.open_screen("indexer")

Delivery is fire-and-forget: :meth:`UiService.post` returns whether an action
was queued, never whether the UI handled it. A missing TUI is normal and is
not an error.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from deeppyro.api import Plugin

#: Action kinds understood by the host TUI.
NOTIFY: Final = "notify"
OPEN_SCREEN: Final = "open_screen"
OPEN_PANEL: Final = "open_panel"
SET_STATUS: Final = "set_status"
ADD_ROW: Final = "add_row"
REFRESH: Final = "refresh"

ACTION_KINDS: Final[tuple[str, ...]] = (NOTIFY, OPEN_SCREEN, OPEN_PANEL, SET_STATUS, ADD_ROW, REFRESH)


@dataclass(frozen=True, slots=True)
class UiAction:
    """One queued UI request.

    Attributes:
        kind: One of :data:`ACTION_KINDS` (unknown kinds are reported by the
            host and never crash the drain loop).
        payload: Small JSON-like mapping interpreted by the host.
    """

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.kind or "").strip():
            raise ValueError("ui action kind must not be empty")


@runtime_checkable
class UiBackend(Protocol):
    """Host side of the action channel (implemented by ``DeepPyroApp``)."""

    def handle_ui_action(self, action: UiAction) -> None:
        """Handle one action on the UI thread."""


def _coerce_action(
    action: UiAction | Mapping[str, Any] | str,
    payload: Mapping[str, Any],
) -> UiAction | None:
    """Normalize the three accepted `post` spellings, or return None."""
    if isinstance(action, UiAction):
        merged = dict(action.payload)
        merged.update(payload)
        return UiAction(action.kind, merged)
    if isinstance(action, str):
        kind = action.strip()
        return UiAction(kind, dict(payload)) if kind else None
    if isinstance(action, Mapping):
        raw_kind = action.get("kind")
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            return None
        merged = {key: value for key, value in action.items() if key not in ("kind", "payload")}
        nested = action.get("payload")
        if isinstance(nested, Mapping):
            merged.update(nested)
        merged.update(payload)
        return UiAction(raw_kind.strip(), merged)
    return None


class UiService(Plugin):
    """Queue one-way UI actions from any thread to the Textual app thread."""

    provide = "ui"
    plugin_id = "ui"
    version = "0.1.0"
    description = "Thread-safe UI actions for plugins (queue drained on the TUI thread)"

    def __init__(self, ctx: Any = None, config: Any = None) -> None:
        super().__init__(ctx, config)
        self._lock = threading.Lock()
        self._queue: deque[UiAction] = deque()
        self._backend: UiBackend | None = None
        self._delivered = 0
        self._dropped = 0
        self._errors: list[str] = []

    def setup(self) -> None:
        """Provide the service; the TUI binds a backend when it mounts."""
        self.ctx.provide("ui", self)

    # -- backend lifecycle -----------------------------------------------
    def bind(self, backend: UiBackend | None) -> None:
        """Attach (or detach) the host that drains actions on the UI thread."""
        with self._lock:
            self._backend = backend

    def unbind(self) -> None:
        """Detach the host; queued actions stay until :meth:`clear`."""
        self.bind(None)

    @property
    def available(self) -> bool:
        """Whether a TUI backend is currently attached."""
        with self._lock:
            return self._backend is not None

    @property
    def pending(self) -> int:
        """Number of actions waiting for the next drain."""
        with self._lock:
            return len(self._queue)

    # -- producer side ----------------------------------------------------
    def post(self, action: UiAction | Mapping[str, Any] | str, /, **payload: Any) -> bool:
        """Enqueue one action; return False when no backend is attached.

        Invalid spellings are recorded as errors and return False instead of
        raising, so a background thread can never crash a plugin turn.
        """
        try:
            normalized = _coerce_action(action, payload)
        except Exception:  # noqa: BLE001 - malformed payload is a plugin bug, not a crash
            normalized = None
        if normalized is None:
            with self._lock:
                self._errors.append(f"invalid ui action: {action!r}")
            return False
        with self._lock:
            if self._backend is None:
                self._dropped += 1
                return False
            self._queue.append(normalized)
            return True

    def notify(self, text: str, level: str = "info") -> bool:
        """Ask the host to show a toast (level: info/success/warning/error)."""
        return self.post(NOTIFY, text=str(text), level=str(level or "info"))

    def open_screen(self, slot_id: str | None = None) -> bool:
        """Ask the host to open a `screen` slot (first screen when omitted)."""
        return self.post(OPEN_SCREEN, slot_id=str(slot_id or ""))

    def open_panel(self, slot_id: str | None = None) -> bool:
        """Ask the host to open a `panel` slot (first panel when omitted)."""
        return self.post(OPEN_PANEL, slot_id=str(slot_id or ""))

    def set_status(self, slot_id: str, text: str) -> bool:
        """Set (or clear, with empty text) one plugin status segment."""
        return self.post(SET_STATUS, id=str(slot_id), text=str(text))

    def add_row(self, text: str, *, level: str = "info") -> bool:
        """Append one conversation row (level: info/error)."""
        return self.post(ADD_ROW, text=str(text), level=str(level or "info"))

    def refresh(self) -> bool:
        """Ask the host to re-render plugin chrome."""
        return self.post(REFRESH)

    # -- consumer side ----------------------------------------------------
    def drain(self, limit: int = 200) -> int:
        """Deliver queued actions to the backend in FIFO order.

        Must be called on the UI thread. One failing action is recorded and
        skipped; it never blocks the rest of the queue. Returns the number of
        actions the backend handled successfully.
        """
        if limit <= 0:
            return 0
        with self._lock:
            backend = self._backend
            if backend is None:
                self._dropped += len(self._queue)
                self._queue.clear()
                return 0
            batch: list[UiAction] = []
            while self._queue and len(batch) < limit:
                batch.append(self._queue.popleft())
        delivered = 0
        for action in batch:
            try:
                backend.handle_ui_action(action)
            except Exception as exc:  # noqa: BLE001 - isolate one bad action
                with self._lock:
                    self._errors.append(f"{action.kind}: {type(exc).__name__}: {exc}")
                continue
            delivered += 1
        with self._lock:
            self._delivered += delivered
        return delivered

    def clear(self) -> int:
        """Drop every queued action; returns how many were discarded."""
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            self._dropped += count
            return count

    # -- diagnostics ------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        """Counter snapshot used by tests and the `/plugins` health view."""
        with self._lock:
            return {
                "bound": self._backend is not None,
                "queued": len(self._queue),
                "delivered": self._delivered,
                "dropped": self._dropped,
                "errors": len(self._errors),
            }

    @property
    def error_messages(self) -> tuple[str, ...]:
        """Recorded failures from invalid posts or backend exceptions."""
        with self._lock:
            return tuple(self._errors)


def resolve_ui_service(source: Any) -> UiService | None:
    """Return the `ui` service from an app, runtime, or service host."""
    if isinstance(source, UiService):
        return source
    if source is None:
        return None
    kernel = getattr(source, "kernel_app", None)
    if kernel is None:
        kernel = source
    service = getattr(kernel, "service", None)
    if not callable(service):
        return None
    try:
        value = service("ui")
    except Exception:  # noqa: BLE001 - optional service, missing is normal
        return None
    return value if isinstance(value, UiService) else None


__all__ = [
    "ACTION_KINDS",
    "ADD_ROW",
    "NOTIFY",
    "OPEN_PANEL",
    "OPEN_SCREEN",
    "REFRESH",
    "SET_STATUS",
    "UiAction",
    "UiBackend",
    "UiService",
    "resolve_ui_service",
]
