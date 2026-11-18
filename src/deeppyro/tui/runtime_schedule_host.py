"""Schedule delivery host for the TUI runtime: routes scheduled output into sessions."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from deeppyro.core.errors import DeeppyroError
from deeppyro.schedule import DeliveryReceipt, NewOnceTarget, NewTarget, SessionTarget
from deeppyro.schedule.timeutil import format_instant
from deeppyro.tui.runtime_support import _session_header_id, await_maybe

if TYPE_CHECKING:
    from deeppyro.tui.runtime import TuiRuntime


class _ScheduleSink:
    """EventSink that discards events from background scheduled turns."""

    def begin_turn(self) -> None:
        return None

    def end_turn(self) -> None:
        return None

    def emit(self, event: dict[str, Any]) -> None:
        return None


def _now_rfc3339() -> str:
    return format_instant(datetime.now(UTC))


class _TuiScheduleHost:
    """ScheduleHost that delivers occurrences into TUI sessions."""

    def __init__(self, runtime: TuiRuntime) -> None:
        self._runtime = runtime

    async def session_exists(self, session_id: str) -> bool:
        runtime = self._runtime
        if not session_id:
            return False
        if session_id == runtime.session_id:
            return True
        exists = getattr(runtime.sessions, "native_exists", None)
        if callable(exists):
            try:
                return bool(exists(session_id))
            except Exception:  # noqa: BLE001 - an unreadable session cannot receive messages
                return False
        return False

    async def delegation_depth(self, session_id: str) -> int:
        return 0

    async def deliver(self, task: Any, message: str) -> DeliveryReceipt | None:
        target = task.target
        source = {"kind": "schedule", "taskId": task.id, "occurrence": task.scheduled_at}
        if isinstance(target, SessionTarget):
            return await self._deliver_to_session(target.session_id, task, message, source)
        if isinstance(target, (NewOnceTarget, NewTarget)):
            return await self._deliver_to_new(target, task, message, source)
        raise DeeppyroError(f"unsupported schedule target: {target!r}")

    async def _deliver_to_session(
        self,
        session_id: str,
        task: Any,
        message: str,
        source: dict[str, Any],
    ) -> DeliveryReceipt:
        return await self._deliver_to_native_session(session_id, message, source)

    async def _deliver_to_native_session(
        self,
        session_id: str,
        message: str,
        source: dict[str, Any],
    ) -> DeliveryReceipt:
        """Deliver into a native session: live agent or a rebuilt silent one."""
        runtime = self._runtime
        if session_id == runtime.session_id and runtime.agent is not None:
            notice = runtime.schedule_notice
            if notice is not None:
                # a notice callback must not break delivery
                with contextlib.suppress(Exception):
                    notice(message)
            runtime.start_schedule_send(runtime.agent, message, source)
            return DeliveryReceipt(
                scheduled_at="",
                delivered_at=_now_rfc3339(),
                target_session_id=session_id,
            )
        session = await runtime.open_schedule_session(session_id)
        agent = await runtime.create_schedule_agent(session, preset=runtime.agent_preset)
        try:
            runtime.start_schedule_send(agent, message, source)
        except Exception:
            close = getattr(agent, "aclose", None)
            if callable(close):
                # cleanup is best effort
                with contextlib.suppress(Exception):
                    await await_maybe(close())
            raise
        runtime.track_schedule_agent(session, agent)
        return DeliveryReceipt(
            scheduled_at="",
            delivered_at=_now_rfc3339(),
            target_session_id=_session_header_id(session),
        )

    async def _deliver_to_new(
        self,
        target: Any,
        task: Any,
        message: str,
        source: dict[str, Any],
    ) -> DeliveryReceipt:
        runtime = self._runtime
        spec = target.create
        preset = str(getattr(spec, "agent_preset", "") or runtime.agent_preset)
        session = None
        if isinstance(target, NewOnceTarget):
            session_id = str(target.session_id or runtime._schedule_once.get(task.id, ""))
            if session_id:
                try:
                    session = await runtime.open_schedule_session(session_id)
                except Exception:  # noqa: BLE001 - recreate a missing once-target session
                    session = None
        if session is None:
            session = await runtime.create_schedule_session(spec, preset)
            if isinstance(target, NewOnceTarget):
                runtime._schedule_once[task.id] = _session_header_id(session)
        agent = await runtime.create_schedule_agent(session, preset=preset)
        try:
            runtime.start_schedule_send(agent, message, source)
        except Exception:
            close = getattr(agent, "aclose", None)
            if callable(close):
                # cleanup is best effort
                with contextlib.suppress(Exception):
                    await await_maybe(close())
            raise
        runtime.track_schedule_agent(session, agent)
        return DeliveryReceipt(
            scheduled_at="",
            delivered_at=_now_rfc3339(),
            target_session_id=_session_header_id(session),
        )
