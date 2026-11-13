"""Hook names, modes, and handler type aliases for the runtime hook service.

Payloads are plain dicts. The hook service itself does not define a wire
schema: each call site (the agent loop or the tool pipeline) documents the keys
it passes, and handlers treat the payload as read-mostly data. A waterfall
handler may return a decision object understood by that call site.

The nine names below are fixed. Call sites should dispatch each hook with the
mode listed in HOOK_MODES; the service does not enforce the mode so tests and
advanced users can use serial or emit on any known name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# -- agent lifecycle -------------------------------------------------------
AGENT_PRE_STEP = "agent/pre-step"
AGENT_REQUEST = "agent/request"
AGENT_REQUEST_ERROR = "agent/request-error"
AGENT_TURN_STOPPING = "agent/turn-stopping"
AGENT_ASSISTANT_STREAM = "agent/assistant-stream"
AGENT_STATUS = "agent/status"

# -- tool pipeline ---------------------------------------------------------
TOOLS_PRE_EXECUTE = "tools/pre-execute"
TOOLS_EXECUTE = "tools/execute"
TOOLS_POST_EXECUTE = "tools/post-execute"

HOOK_NAMES = frozenset(
    {
        AGENT_PRE_STEP,
        AGENT_REQUEST,
        AGENT_REQUEST_ERROR,
        AGENT_TURN_STOPPING,
        AGENT_ASSISTANT_STREAM,
        AGENT_STATUS,
        TOOLS_PRE_EXECUTE,
        TOOLS_EXECUTE,
        TOOLS_POST_EXECUTE,
    }
)

# Recommended dispatch mode for each hook. Call sites that follow this table
# keep the DSH semantics: unordered listeners are not called as a waterfall.
HOOK_MODES: dict[str, str] = {
    AGENT_PRE_STEP: "waterfall",
    AGENT_REQUEST: "waterfall",
    AGENT_REQUEST_ERROR: "waterfall",
    AGENT_TURN_STOPPING: "serial",
    AGENT_ASSISTANT_STREAM: "emit",
    AGENT_STATUS: "emit",
    TOOLS_PRE_EXECUTE: "waterfall",
    TOOLS_EXECUTE: "waterfall",
    TOOLS_POST_EXECUTE: "waterfall",
}

HookPayload = dict[str, Any]
HookNext = Callable[[], Awaitable[Any]]
HookHandler = Callable[[HookPayload, HookNext], Any]
SerialHandler = Callable[[HookPayload], Any]
HookDisposer = Callable[[], None]
