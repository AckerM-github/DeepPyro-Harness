"""LLM provider layer: streaming providers, retries and model discovery.

Public API (INTERFACES 5):
  LlmRegistry       - build/cache providers from Config, plus the model catalog
  create_provider   - construct one provider from a ProviderConfig
  OpenAICompatProvider - streaming chat-completions transport
  AnthropicProvider - streaming Anthropic Messages transport
  RetryPolicy       - exponential backoff for retryable first-token failures
  ModelInfo...      - model-catalog values returned by list_models()/model_info()
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
from deeppyro.llm.anthropic import AnthropicProvider
from deeppyro.llm.catalog import (
    ModelBadge,
    ModelCatalog,
    ModelInfo,
    catalog_from_config,
    merge_catalogs,
    parse_model_listing,
)
from deeppyro.llm.discovery import (
    DEFAULT_CATALOG_TIMEOUT_S,
    DEFAULT_CATALOG_TTL_S,
    MAX_LISTING_BYTES,
    CatalogCache,
    anthropic_listing_url,
    fetch_listing,
    openai_listing_url,
)
from deeppyro.llm.errors import (
    LlmHttpError,
    LlmRequestError,
    failure_dict,
)
from deeppyro.llm.messages import build_chat_payload, convert_messages, message_to_chat
from deeppyro.llm.messages_anthropic import (
    REASONING_EFFORTS,
    build_anthropic_payload,
    convert_anthropic_messages,
    tool_to_anthropic,
)
from deeppyro.llm.openai_compat import OpenAICompatProvider
from deeppyro.llm.registry import LlmRegistry, create_provider
from deeppyro.llm.retry import RetryPolicy
from deeppyro.llm.sse import SseDecoder, SseEvent, decode_sse_text, iter_sse

__all__ = [
    "DEFAULT_CATALOG_TIMEOUT_S",
    "DEFAULT_CATALOG_TTL_S",
    "MAX_LISTING_BYTES",
    "REASONING_EFFORTS",
    "AnthropicProvider",
    "CatalogCache",
    "Finish",
    "FinishKind",
    "LlmHttpError",
    "LlmProvider",
    "LlmRegistry",
    "LlmRequest",
    "LlmRequestError",
    "ModelBadge",
    "ModelCatalog",
    "ModelInfo",
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
    "anthropic_listing_url",
    "build_anthropic_payload",
    "build_chat_payload",
    "catalog_from_config",
    "convert_anthropic_messages",
    "convert_messages",
    "create_provider",
    "decode_sse_text",
    "failure_dict",
    "fetch_listing",
    "iter_sse",
    "merge_catalogs",
    "message_to_chat",
    "openai_listing_url",
    "parse_model_listing",
    "tool_to_anthropic",
]
