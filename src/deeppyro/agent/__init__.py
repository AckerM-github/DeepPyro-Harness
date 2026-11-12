"""Agent loop: options and the turn/step session driver.

Event sinks are a frontend concern; the terminal implementations live in
`deeppyro.headless` (one-shot) and `deeppyro.tui.sink` (interactive). The loop only accepts an
injected `EventSink`.
"""

from __future__ import annotations

from deeppyro.agent.options import AgentOptions
from deeppyro.agent.session import (
    AgentSession,
    StreamAccumulator,
    ToolCallDraft,
)

__all__ = [
    "AgentOptions",
    "AgentSession",
    "StreamAccumulator",
    "ToolCallDraft",
]
