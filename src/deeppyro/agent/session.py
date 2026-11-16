"""The agent turn/step loop (P0: inbox, hooks, prompt projections, retries).

Public entry points:

* :meth:`AgentSession.send` enqueues a next-turn message and drives the loop
  until the inbox is empty, returning the final assistant text of the last turn.
* :meth:`AgentSession.followup` / :meth:`AgentSession.steer` /
  :meth:`AgentSession.inject` enqueue messages for the next turn or the next
  step boundary; :meth:`AgentSession.wait_for_idle` waits for the driver.
* :meth:`AgentSession.interrupt` cancels the running turn and (by default)
  clears the pending inbox.
* :meth:`AgentSession.record_hook_event` persists one hook journal record and
  mirrors it to the live sink (durable record first, event second).

A turn is driven by inbox claims, mirroring the reference loop:

    turn/start
      [agent/pre-step waterfall]              (once, before the first step)
      step/start
        system/message (projection)           (each step; append or replace)
        user/message  x claimed messages      (first step)
        user/message  runtime context         (each step; no-op when unchanged)
        request/header request/context        (when the request changed)
        [assistant/message | assistant/attempt]
        tool/call ... -> tool/result ...      (grouped by parallel barrier)
      step/end
      step/start ...
      step/end
      [agent/turn-stopping serial]            (when no next-step is pending)
    turn/end

Events are emitted only after the matching durable record has been appended.
Transient text/thinking frames are emitted live while the provider streams.
"""

from __future__ import annotations

from .session_support import (
    HOOK_RECORD_TYPES,
    TOOL_ABORTED,
    TOOL_ABORTED_BEFORE_DISPATCH,
    SessionHandle,
    StreamAccumulator,
    ToolCallDraft,
)
from .session_turn import TurnLayer

__all__ = [
    "HOOK_RECORD_TYPES",
    "TOOL_ABORTED",
    "TOOL_ABORTED_BEFORE_DISPATCH",
    "AgentSession",
    "SessionHandle",
    "StreamAccumulator",
    "ToolCallDraft",
]


class AgentSession(TurnLayer):
    """Drive one conversation: inbox, turn/step records, provider stream, tools."""
