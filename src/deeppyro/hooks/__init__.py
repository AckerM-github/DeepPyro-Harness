"""Runtime hooks for the agent loop and the tool pipeline.

The service is exposed to plugins and the runtime as the 'hooks' kernel
service. Well-known hook names and payload conventions live in
deeppyro.hooks.types; docs/spec/hooks.md is the contract for call sites.
"""

from __future__ import annotations

from .service import HookAggregateError, HookService
from .types import (
    AGENT_ASSISTANT_STREAM,
    AGENT_ERROR,
    AGENT_PRE_STEP,
    AGENT_REQUEST,
    AGENT_REQUEST_ERROR,
    AGENT_STATUS,
    AGENT_TURN_STOPPING,
    APPROVAL_ASKED,
    APPROVAL_DECIDED,
    APPROVAL_REQUEST,
    HOOK_MODES,
    HOOK_NAMES,
    LLM_STREAM,
    MODEL_SELECTION,
    SESSION_CREATED,
    SESSION_DISPOSED,
    SYSTEM_PROMPT_ASSEMBLE,
    TOOL_CALL,
    TOOLS_EXECUTE,
    TOOLS_POST_EXECUTE,
    TOOLS_PRE_EXECUTE,
    HookDisposer,
    HookHandler,
    HookNext,
    HookPayload,
    SerialHandler,
)

__all__ = [
    "AGENT_ASSISTANT_STREAM",
    "AGENT_ERROR",
    "AGENT_PRE_STEP",
    "AGENT_REQUEST",
    "AGENT_REQUEST_ERROR",
    "AGENT_STATUS",
    "AGENT_TURN_STOPPING",
    "APPROVAL_ASKED",
    "APPROVAL_DECIDED",
    "APPROVAL_REQUEST",
    "HOOK_MODES",
    "HOOK_NAMES",
    "LLM_STREAM",
    "MODEL_SELECTION",
    "SESSION_CREATED",
    "SESSION_DISPOSED",
    "SYSTEM_PROMPT_ASSEMBLE",
    "TOOLS_EXECUTE",
    "TOOLS_POST_EXECUTE",
    "TOOLS_PRE_EXECUTE",
    "TOOL_CALL",
    "HookAggregateError",
    "HookDisposer",
    "HookHandler",
    "HookNext",
    "HookPayload",
    "HookService",
    "SerialHandler",
]
