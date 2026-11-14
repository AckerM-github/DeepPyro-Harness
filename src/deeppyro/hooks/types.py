"""Hook names, modes, and handler type aliases for the runtime hook service.

Payloads are plain dicts when a call site dispatches them. HookService freezes
the payload recursively before every dispatch: mappings become read-only
types.MappingProxyType, lists (and tuples) become tuples, and custom objects (an
agent, a signal event, a request object) keep their identity and are not
traversed. Handlers therefore treat the payload as read-only data. A waterfall
handler may return a decision object understood by that call site, optionally
after calling the next() step.

The names below are fixed. Call sites should dispatch each hook with the mode
listed in HOOK_MODES; the service does not enforce the mode so tests and
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

# -- prompt and llm --------------------------------------------------------
SYSTEM_PROMPT_ASSEMBLE = "system-prompt/assemble"
LLM_STREAM = "llm/stream"
MODEL_SELECTION = "model/selection"

# -- session lifecycle -----------------------------------------------------
SESSION_CREATED = "session/created"
SESSION_DISPOSED = "session/disposed"
AGENT_ERROR = "agent/error"

# -- tool pipeline ---------------------------------------------------------
TOOLS_PRE_EXECUTE = "tools/pre-execute"
TOOLS_EXECUTE = "tools/execute"
TOOLS_POST_EXECUTE = "tools/post-execute"
TOOL_CALL = "tool/call"

# -- approval --------------------------------------------------------------
APPROVAL_REQUEST = "approval/request"
APPROVAL_ASKED = "approval/asked"
APPROVAL_DECIDED = "approval/decided"

HOOK_NAMES = frozenset(
    {
        AGENT_PRE_STEP,
        AGENT_REQUEST,
        AGENT_REQUEST_ERROR,
        AGENT_TURN_STOPPING,
        AGENT_ASSISTANT_STREAM,
        AGENT_STATUS,
        SYSTEM_PROMPT_ASSEMBLE,
        LLM_STREAM,
        MODEL_SELECTION,
        SESSION_CREATED,
        SESSION_DISPOSED,
        AGENT_ERROR,
        TOOLS_PRE_EXECUTE,
        TOOLS_EXECUTE,
        TOOLS_POST_EXECUTE,
        TOOL_CALL,
        APPROVAL_REQUEST,
        APPROVAL_ASKED,
        APPROVAL_DECIDED,
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
    SYSTEM_PROMPT_ASSEMBLE: "waterfall",
    LLM_STREAM: "emit",
    MODEL_SELECTION: "waterfall",
    SESSION_CREATED: "emit",
    SESSION_DISPOSED: "emit",
    AGENT_ERROR: "emit",
    TOOLS_PRE_EXECUTE: "waterfall",
    TOOLS_EXECUTE: "waterfall",
    TOOLS_POST_EXECUTE: "waterfall",
    TOOL_CALL: "emit",
    APPROVAL_REQUEST: "waterfall",
    APPROVAL_ASKED: "emit",
    APPROVAL_DECIDED: "emit",
}

HookPayload = dict[str, Any]
HookNext = Callable[[], Awaitable[Any]]
HookHandler = Callable[[HookPayload, HookNext], Any]
SerialHandler = Callable[[HookPayload], Any]
HookDisposer = Callable[[], None]
