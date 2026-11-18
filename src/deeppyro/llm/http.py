"""Shared HTTP plumbing for OpenAI-compatible providers.

Both the v4 `OpenAICompatProvider` and the native
`NativeOpenAICompatProvider` speak to the same servers, so the transport
configuration (timeout shape, SSE request headers, bearer auth) lives here
instead of being duplicated.  Secrets never come from this module: callers pass
the already-resolved API key from `ProviderConfig.resolve_api_key`, which stays
the only environment/inline secret reader.
"""

from __future__ import annotations

import httpx

__all__ = ["build_client", "headers"]


def build_client(timeout_s: float) -> httpx.AsyncClient:
    """Create the streaming HTTP client with the standard timeout shape."""
    timeout = httpx.Timeout(timeout_s, connect=min(30.0, timeout_s))
    return httpx.AsyncClient(timeout=timeout)


def headers(api_key: str | None) -> dict[str, str]:
    """Request headers for a streaming chat-completions call."""
    result = {"content-type": "application/json", "accept": "text/event-stream"}
    if api_key:
        result["authorization"] = f"Bearer {api_key}"
    return result
