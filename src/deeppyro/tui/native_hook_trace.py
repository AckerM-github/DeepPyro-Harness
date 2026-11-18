"""Hook registry and recent-decision recorder for the native TUI runtime.

Native sessions record no hook journal: schema 1 has no hook/invoked or
hook/result events, and the native host dispatches the same HookService that the
plugins use. So /hooks observes the live hook service:
this recorder is registered with HookService.add_observer, which fires for
every dispatch (invoked before, resolved after) even when no listener exists.

Nothing here invents a decision. If the hook service is unavailable, or a
dispatch carries no payload/duration, the UI says "not recorded" instead.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any, cast

from deeppyro.hooks import HOOK_MODES, HOOK_NAMES

__all__ = ["NOT_RECORDED", "UNAVAILABLE_TEXT", "NativeHookRecorder"]

#: How many recent decisions the recorder keeps (matches the historical /hooks limit).
DEFAULT_LIMIT = 50

#: Explicit placeholder shown when the runtime never observed a hook decision.
NOT_RECORDED = "not recorded"

#: Text shown when the hook service itself is unavailable in this bundle.
UNAVAILABLE_TEXT = "native hook registry: not recorded (the hook service is unavailable in this bundle)"


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


class NativeHookRecorder:
    """Records HookService dispatches and reports the live hook registry.

    Every dispatch becomes one row shaped like screens.hook_trace expects
    ({'hook', 'mode', 'invoked', 'result'}). The registry view reports the
    declared hooks, their
    dispatch mode, listener counts and observed invocation counts.
    """

    def __init__(self, service: Any, *, limit: int = DEFAULT_LIMIT) -> None:
        self._service = service
        self._limit = max(1, _int(limit))
        self._rows: list[dict[str, Any]] = []
        self._counts: dict[str, dict[str, int]] = {}
        self._disposer: Any = None
        add_observer = getattr(service, "add_observer", None)
        if callable(add_observer):
            self._disposer = add_observer(self._observe)

    # -- recording -------------------------------------------------------
    def _trim(self) -> None:
        overflow = len(self._rows) - self._limit
        if overflow > 0:
            del self._rows[:overflow]

    def _counts_for(self, hook: str) -> dict[str, int]:
        entry = self._counts.get(hook)
        if entry is None:
            entry = {"invoked": 0, "resolved": 0, "errors": 0}
            self._counts[hook] = entry
        return entry

    def _observe(self, observation: Any) -> None:
        """HookService observer callback (must never raise into the dispatcher)."""
        # observation must never break a dispatch
        with contextlib.suppress(Exception):
            self._record(observation)

    def _record(self, observation: Any) -> None:
        hook = str(getattr(observation, "hook", "") or "").strip()
        if not hook:
            return
        kind = str(getattr(observation, "kind", "") or "").strip()
        mode = str(getattr(observation, "mode", "") or "").strip() or str(HOOK_MODES.get(hook, "") or "")
        counts = self._counts_for(hook)
        if kind == "invoked":
            counts["invoked"] += 1
            self._rows.append(
                {
                    "hook": hook,
                    "mode": mode,
                    "invoked": {"mode": mode, "recorded": True},
                    "result": None,
                }
            )
            self._trim()
            return
        if kind != "resolved":
            return
        counts["resolved"] += 1
        error = getattr(observation, "error", None)
        if error is not None:
            counts["errors"] += 1
        duration = getattr(observation, "duration_ms", None)
        result: dict[str, Any] = {
            "ok": error is None,
            "recorded": True,
            "durationMs": float(duration) if isinstance(duration, (int, float)) else None,
        }
        if error is not None:
            result["error"] = str(error)
        for row in reversed(self._rows):
            if row.get("hook") == hook and row.get("result") is None:
                row["result"] = result
                return
        self._rows.append({"hook": hook, "mode": mode, "invoked": None, "result": result})
        self._trim()

    # -- views -----------------------------------------------------------
    def rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Recent paired hook decisions, oldest first, capped at limit."""
        rows = self._rows
        if limit is not None and limit >= 0:
            rows = rows[-limit:] if limit else []
        return [dict(row) for row in rows]

    def _listener_count(self, hook: str) -> int | None:
        listeners = getattr(self._service, "listeners", None)
        if not callable(listeners):
            return None
        try:
            return len(tuple(cast(Iterable[Any], listeners(hook))))
        except Exception:  # noqa: BLE001 - the registry view is best effort
            return None

    def registry_rows(self) -> list[dict[str, Any]]:
        """One row per declared hook plus any observed hook outside the set."""
        rows: list[dict[str, Any]] = []
        known = set(HOOK_NAMES)
        for hook in HOOK_NAMES:
            counts = self._counts.get(hook, {})
            rows.append(
                {
                    "hook": hook,
                    "mode": str(HOOK_MODES.get(hook, "") or ""),
                    "listeners": self._listener_count(hook),
                    "invoked": _int(counts.get("invoked")),
                    "resolved": _int(counts.get("resolved")),
                    "errors": _int(counts.get("errors")),
                }
            )
        for hook, counts in self._counts.items():
            if hook in known:
                continue
            rows.append(
                {
                    "hook": hook,
                    "mode": str(HOOK_MODES.get(hook, "") or ""),
                    "listeners": self._listener_count(hook),
                    "invoked": _int(counts.get("invoked")),
                    "resolved": _int(counts.get("resolved")),
                    "errors": _int(counts.get("errors")),
                }
            )
        return rows

    def registry_summary(self) -> str:
        """One-line summary of the native hook registry state."""
        rows = self.registry_rows()
        hooked = sum(1 for row in rows if (row.get("listeners") or 0) > 0)
        observed = sum(1 for row in rows if (row.get("invoked") or 0) > 0)
        return (
            f"native hook registry: {len(rows)} hooks · {hooked} with listeners · "
            f"{observed} observed · {len(self._rows)} recent decision(s)"
        )

    def registry_lines(self) -> list[str]:
        """Renderable registry lines: summary plus one line per hook."""
        rows = self.registry_rows()
        lines = [self.registry_summary()]
        for row in rows:
            listeners = row.get("listeners")
            listener_text = f"listeners={listeners}" if listeners is not None else f"listeners={NOT_RECORDED}"
            lines.append(
                "  "
                + " · ".join(
                    (
                        str(row.get("hook", "")),
                        f"mode={row.get('mode') or NOT_RECORDED}",
                        listener_text,
                        f"invoked={row.get('invoked', 0)}",
                        f"resolved={row.get('resolved', 0)}",
                        f"errors={row.get('errors', 0)}",
                    )
                )
            )
        if not any((row.get("invoked") or 0) > 0 for row in rows):
            lines.append(f"  recent hook decisions: {NOT_RECORDED} yet")
        return lines

    def close(self) -> None:
        """Detach the observer (best effort)."""
        disposer = self._disposer
        self._disposer = None
        if callable(disposer):
            # shutdown must continue even if the service is already torn down
            with contextlib.suppress(Exception):
                disposer()
