"""Shared runtime contracts: errors, ids and the tool API.

Nothing in this package may be changed by a workstream without Lead sign-off.
"""

from deeppyro.core.errors import (
    AbortedError,
    ConfigError,
    DeeppyroError,
    LlmError,
    PermissionDenied,
    SessionError,
    ToolError,
)
from deeppyro.core.ids import new_call_id, new_message_id, new_session_id, now_ms
from deeppyro.core.protocols import Tool, ToolContext, ToolResult, ToolSchema

__all__ = [
    "AbortedError",
    "ConfigError",
    "DeeppyroError",
    "LlmError",
    "PermissionDenied",
    "SessionError",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolSchema",
    "new_call_id",
    "new_message_id",
    "new_session_id",
    "now_ms",
]
