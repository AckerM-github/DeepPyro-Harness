"""Frozen shared contracts: wire models and runtime protocols.

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
from deeppyro.core.protocols import (
    EventSink,
    Finish,
    FinishKind,
    LlmProvider,
    LlmRequest,
    ReasoningDelta,
    SessionLogLike,
    StreamEvent,
    TextDelta,
    Tool,
    ToolCallArgsDelta,
    ToolCallStart,
    ToolContext,
    ToolResult,
    ToolSchema,
    Usage,
)
from deeppyro.core.wire import (
    MESSAGE_PROJECTION_TYPES,
    RECORD_TYPES,
    SURFACE_EVENT_TYPES,
    ContentPart,
    ImagePart,
    LogRecord,
    Message,
    ReasoningPart,
    SessionHeader,
    TextPart,
    ToolAdditionPart,
    ToolCallPart,
    ToolRemovalPart,
    UnknownPart,
    ReplaceOp,
    WireModel,
)

__all__ = [
    "AbortedError", "ConfigError", "DeeppyroError", "LlmError", "PermissionDenied",
    "SessionError", "ToolError",
    "new_call_id", "new_message_id", "new_session_id", "now_ms",
    "EventSink", "Finish", "FinishKind", "LlmProvider", "LlmRequest", "ReasoningDelta",
    "SessionLogLike", "StreamEvent", "TextDelta", "Tool", "ToolCallArgsDelta",
    "ToolCallStart", "ToolContext", "ToolResult", "ToolSchema", "Usage",
    "ContentPart", "ImagePart", "LogRecord", "Message", "ReasoningPart", "SessionHeader",
    "TextPart", "ToolAdditionPart", "ToolCallPart", "ToolRemovalPart", "UnknownPart", "WireModel",
    "ReplaceOp", "RECORD_TYPES", "SURFACE_EVENT_TYPES", "MESSAGE_PROJECTION_TYPES",
]
