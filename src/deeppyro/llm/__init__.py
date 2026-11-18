"""Native-only LLM layer: wire-free providers, retries and model discovery.

Public API:
  LlmRegistry            - build/cache native providers from Config, plus the model catalog
  create_native_provider - construct one native provider from a ProviderConfig
  ModelRequest/ModelResponse/StreamEvent/... - the deeppyro.core.llm contract, re-exported
  RetryPolicy            - exponential backoff for retryable first-token failures
  ModelInfo...           - model-catalog values returned by list_models()/model_info()
  errors/discovery/SSE   - helpers shared by native provider plugins and tooling

Providers stream `deeppyro.core.llm.ModelRequest` and yield `deeppyro.core.llm.StreamEvent`
values; there is no second (v4) transport layer behind this package.
"""

from __future__ import annotations

from deeppyro.core.llm import (
    Finish,
    LlmProvider,
    ModelRequest,
    ModelResponse,
    NativeToolCall,
    ReasoningDelta,
    RetryScheduled,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    ToolSpec,
    Usage,
    UsageEvent,
)
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
from deeppyro.llm.registry import LlmRegistry, create_native_provider
from deeppyro.llm.retry import RetryPolicy
from deeppyro.llm.sse import SseDecoder, SseEvent, decode_sse_text, iter_sse

__all__ = [
    "DEFAULT_CATALOG_TIMEOUT_S",
    "DEFAULT_CATALOG_TTL_S",
    "MAX_LISTING_BYTES",
    "CatalogCache",
    "Finish",
    "LlmHttpError",
    "LlmProvider",
    "LlmRegistry",
    "LlmRequestError",
    "ModelBadge",
    "ModelCatalog",
    "ModelInfo",
    "ModelRequest",
    "ModelResponse",
    "NativeToolCall",
    "ReasoningDelta",
    "RetryPolicy",
    "RetryScheduled",
    "SseDecoder",
    "SseEvent",
    "StopReason",
    "StreamEvent",
    "TextDelta",
    "ToolCallArgsDelta",
    "ToolCallStart",
    "ToolSpec",
    "Usage",
    "UsageEvent",
    "anthropic_listing_url",
    "catalog_from_config",
    "create_native_provider",
    "decode_sse_text",
    "failure_dict",
    "fetch_listing",
    "iter_sse",
    "merge_catalogs",
    "openai_listing_url",
    "parse_model_listing",
]
