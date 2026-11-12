"""LLM provider layer: streaming OpenAI-compatible providers with retries.

Public API (INTERFACES 5):
  LlmRegistry       - build/cache providers from Config
  create_provider   - construct one provider from a ProviderConfig
  OpenAICompatProvider - streaming chat-completions transport
  RetryPolicy       - exponential backoff for retryable first-token failures
  StreamEvent...    - re-exported core protocol event types for convenience
"""

from __future__ import annotations

from deeppyro.core.protocols import (
    Finish,
    FinishKind,
    LlmProvider,
    LlmRequest,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
)
from deeppyro.llm.errors import (
    LlmHttpError,
    LlmRequestError,
    failure_dict,
)
from deeppyro.llm.messages import build_chat_payload, convert_messages, message_to_chat
from deeppyro.llm.openai_compat import OpenAICompatProvider
from deeppyro.llm.registry import LlmRegistry, create_provider
from deeppyro.llm.retry import RetryPolicy
from deeppyro.llm.sse import SseDecoder, SseEvent, decode_sse_text, iter_sse

__all__ = [
    "Finish",
    "FinishKind",
    "LlmHttpError",
    "LlmProvider",
    "LlmRegistry",
    "LlmRequest",
    "LlmRequestError",
    "OpenAICompatProvider",
    "ReasoningDelta",
    "RetryPolicy",
    "SseDecoder",
    "SseEvent",
    "StreamEvent",
    "TextDelta",
    "ToolCallArgsDelta",
    "ToolCallStart",
    "Usage",
    "build_chat_payload",
    "convert_messages",
    "create_provider",
    "decode_sse_text",
    "failure_dict",
    "iter_sse",
    "message_to_chat",
]
