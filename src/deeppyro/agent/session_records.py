"""Record, event and turn-inspection helpers for the agent session."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord, Message
from deeppyro.session.derive import repair_pending_tool_calls

from .session_state import _SessionState

if TYPE_CHECKING:
    from deeppyro.core.payloads import ToolCallPayload


class RecordLayer(_SessionState):
    """Record, event and turn-inspection helpers for the agent session."""

    def _append(self, record_type: str, data: dict[str, Any], **envelope: Any) -> int:
        if self._append_supports_envelope and envelope:
            return int(self._session.append(record_type, data, **envelope))
        return int(self._session.append(record_type, data))

    def _emit_status(self, phase: str) -> None:
        self._emit({"type": "status", "phase": phase})

    def current_turn_records(self) -> list[LogRecord]:
        """The records appended by the open turn, in order (empty when idle)."""
        records = self._records()
        start = None
        for index, record in enumerate(records):
            if record.type != "turn/start":
                continue
            data = record.data or {}
            if data.get("turn") == self._turn:
                start = index
                end = len(records)
                for later in range(index + 1, len(records)):
                    if records[later].type == "turn/start":
                        end = later
                        break
                return list(records[start:end])
        return [] if start is None else list(records[start:])

    def current_turn_human_input(self) -> bool:
        """Did a human speak in the open turn?"""
        for record in self.current_turn_records():
            if record.type != "user/message":
                continue
            data = record.data or {}
            source = data.get("source") or {}
            if isinstance(source, dict) and source.get("kind") == "user":
                return True
        return False

    def current_turn_goal_round(self) -> dict[str, Any] | None:
        """The goal round that admitted this turn, if any."""
        for record in self.current_turn_records():
            if record.type != "user/message":
                continue
            data = record.data or {}
            source = data.get("source") or {}
            if isinstance(source, dict) and source.get("kind") == "goal":
                return {
                    "goalId": source.get("goalId"),
                    "revision": source.get("revision"),
                    "round": source.get("round"),
                }
        return None

    def _derive_messages(self) -> list[Message]:
        derive = getattr(self._session, "derive_messages", None)
        if not callable(derive):
            raise SessionError("session object has no derive_messages()")
        return repair_pending_tool_calls(list(cast(Iterable[Any], derive())))

    def _count_turns(self) -> int:
        return sum(1 for record in self._records() if record.type == "turn/start")

    def _tool_log(self, record_type: str, data: dict[str, Any]) -> int:
        return self._append(record_type, data)

    def _tool_call_log(self, turn: int | None, step: int | None, payload: ToolCallPayload) -> int:
        """Append one tool/call record (called by the tools pipeline) and emit its event."""
        data = {
            "turn": turn,
            "step": step,
            "callId": payload["callId"],
            "name": payload["name"],
            "arguments": payload["arguments"],
        }
        seq = self._append("tool/call", data)
        self._call_seqs[str(payload["callId"])] = seq
        self._emit(
            {
                "type": "tool_call",
                "callId": payload["callId"],
                "name": payload["name"],
                "arguments": payload["arguments"],
            }
        )
        return seq
