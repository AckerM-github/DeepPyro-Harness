"""Layer 2 of the DeepPyro TUI app: Agent event rendering layer (the TuiSink target)."""

from __future__ import annotations

import contextlib
from typing import Any

from deeppyro.tui.app_status import StatusLayer
from deeppyro.tui.slots import apply_conversation_slots, build_slot_context


class EventLayer(StatusLayer):
    """Agent event rendering layer (the TuiSink target)."""

    # -- agent events ----------------------------------------------------
    def handle_event(self, event: dict[str, Any]) -> None:
        """Render one agent event (called by TuiSink on the UI loop)."""
        kind = str(event.get("type") or "")
        # rendering must never kill a turn
        with contextlib.suppress(Exception):
            conversation = self.conversation
            if kind == "text":
                conversation.stream_text(str(event.get("text") or ""))
            elif kind == "thinking":
                conversation.stream_thinking(str(event.get("text") or ""))
            elif kind == "step":
                conversation.begin_step(int(event.get("turn") or 0), int(event.get("step") or 0))
            elif kind == "tool_call":
                conversation.add_tool_call(
                    str(event.get("callId") or ""),
                    str(event.get("name") or ""),
                    str(event.get("arguments") or ""),
                )
            elif kind == "tool_result":
                conversation.add_tool_result(
                    str(event.get("callId") or ""),
                    bool(event.get("isError")),
                    str(event.get("text") or ""),
                )
            elif kind == "usage":
                self._context_used = int(event.get("input") or 0)
            elif kind == "final":
                conversation.finish_turn(str(event.get("text") or ""))
            elif kind == "error":
                conversation.finish_turn(None)
                conversation.add_error(str(event.get("message") or "unknown error"))
            elif kind == "status":
                self._phase = str(event.get("phase") or "idle")
            apply_conversation_slots(conversation, event, self._slots, context=build_slot_context(self))
        self._refresh_activity()
        self.refresh_status()
