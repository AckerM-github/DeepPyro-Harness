"""Discovery of the models a provider advertises (/v1/models).

DSH treats a model listing as *optional evidence*: it is fetched lazily, cached briefly,
and never trusted over the configured ProviderConfig. Any failure (offline gateway, 404,
HTML login page, oversized payload) must degrade to the configured catalog rather than
breaking the session; that fallback decision lives in LlmRegistry, while this module
provides the transport plus the small TTL cache the registry drives.

Design notes (docs/plan/p1-providers.md, docs/spec/llm.md section 13):

* Listings are not streamed and must not hang a UI, hence the short 5 s timeout instead
  of the provider's 600 s stream timeout.
* Redirects are refused (follow_redirects=False): a redirect usually means "wrong base
  URL" (e.g. http -> https or a missing /v1) and following it silently could hit a login
  page. The caller reports the HTTP failure and keeps the configured catalog.
* Payload cap 4 MiB: a gateway returning an unbounded body must not exhaust memory.
* Cache TTL 600 s: the DSH TUI refreshes on demand; a 10 minute window keeps selection
  lists stable without hammering the endpoint. Failed refreshes reuse the last good
  listing (see CatalogCache.store_error) so a flaky gateway does not make the model list
  flicker, and they back off for another TTL.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from deeppyro.core.errors import LlmError
from deeppyro.llm.catalog import ModelInfo
from deeppyro.llm.errors import (
    CODE_PROTOCOL,
    CODE_TIMEOUT,
    CODE_TRANSPORT,
    LlmRequestError,
    http_error,
    shorten,
)

#: Listings are interactive metadata: fail fast instead of the 600 s stream default.
DEFAULT_CATALOG_TIMEOUT_S = 5.0

#: How long a successful listing is reused without refetching.
DEFAULT_CATALOG_TTL_S = 600.0

#: Anthropic requires anthropic-version on every request, including discovery.
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

#: /v1/models page size for Anthropic (the endpoint caps at 1000).
ANTHROPIC_MODEL_LIMIT = 1000

#: Hard cap on a listing payload; anything larger is treated as a protocol failure.
MAX_LISTING_BYTES = 4 * 1024 * 1024

OPENAI_LISTING_SUFFIX = "/models"


def openai_listing_url(base_url: str) -> str:
    """Derive the OpenAI-compatible listing URL from a configured base URL.

    Accepts both a base ("http://host/v1") and a full chat endpoint
    ("https://api.deepseek.com/v1/chat/completions"); the endpoint suffix is dropped so a
    user that pasted the completions URL still gets a working discovery URL.
    """
    base = base_url.strip().rstrip("/")
    if base.endswith(OPENAI_LISTING_SUFFIX):
        return base
    for suffix in ("/chat/completions", "/completions"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base + OPENAI_LISTING_SUFFIX


def anthropic_listing_url(base_url: str) -> str:
    """Derive the Anthropic models URL from a configured base URL.

    Anthropic bases in the wild: "https://api.anthropic.com", ".../v1",
    ".../v1/messages" (dsh-llm-deepseek style). Normalize all of them to
    "<origin>/v1/models?limit=1000".
    """
    base = base_url.strip().rstrip("/")
    for suffix in ("/v1/models", OPENAI_LISTING_SUFFIX, "/v1/messages", "/messages", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}/v1/models?limit={ANTHROPIC_MODEL_LIMIT}"


async def fetch_listing(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    provider: str = "",
    timeout_s: float = DEFAULT_CATALOG_TIMEOUT_S,
) -> Any:
    """GET a model listing and return the decoded JSON payload.

    Raises LlmRequestError subclasses for every failure mode; callers are expected to
    catch LlmError and fall back to the configured catalog.
    """
    try:
        response = await client.get(
            url,
            headers=dict(headers or {}),
            timeout=timeout_s,
            follow_redirects=False,
        )
    except httpx.TimeoutException as exc:
        raise LlmRequestError(
            f"{provider}: model listing timed out after {timeout_s:g}s ({url}): {exc}",
            provider=provider or None,
            retryable=True,
            code=CODE_TIMEOUT,
        ) from exc
    except httpx.HTTPError as exc:
        raise LlmRequestError(
            f"{provider}: model listing transport error ({url}): {exc}",
            provider=provider or None,
            retryable=True,
            code=CODE_TRANSPORT,
        ) from exc

    if response.status_code >= 300:
        # 3xx is reported rather than followed: a redirect on a models URL usually means
        # the configured base URL is wrong, and following it may land on a login page.
        body = _body_text(response)
        raise http_error(
            provider or "provider",
            response.status_code,
            headers=response.headers,
            body=body,
        )

    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_LISTING_BYTES:
                raise _oversized(provider, url, int(declared))
        except ValueError:
            pass
    body_bytes = response.content
    if len(body_bytes) > MAX_LISTING_BYTES:
        raise _oversized(provider, url, len(body_bytes))

    try:
        return json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        preview = shorten(_body_text(response), 120)
        raise LlmRequestError(
            f"{provider}: model listing is not JSON ({url}): {preview}",
            provider=provider or None,
            retryable=True,
            status=response.status_code,
            code=CODE_PROTOCOL,
        ) from exc


def _oversized(provider: str, url: str, size: int) -> LlmRequestError:
    return LlmRequestError(
        f"{provider}: model listing from {url} is {size} bytes "
        f"(limit {MAX_LISTING_BYTES}); refusing to parse it",
        provider=provider or None,
        retryable=False,
        code=CODE_PROTOCOL,
    )


def _body_text(response: httpx.Response) -> str:
    try:
        return response.text
    except (LookupError, UnicodeDecodeError):  # pragma: no cover - exotic charset
        return ""


@dataclass(slots=True)
class _CatalogEntry:
    models: list[ModelInfo]
    fetched_at: float


@dataclass(slots=True)
class CatalogCache:
    """Per-provider model listing cache with TTL and last-known-good semantics.

    The clock is injectable so tests can drive TTL expiry without sleeping.
    """

    ttl_s: float = DEFAULT_CATALOG_TTL_S
    clock: Callable[[], float] = time.monotonic
    _entries: dict[str, _CatalogEntry] = field(default_factory=dict, repr=False)
    _errors: dict[str, LlmError] = field(default_factory=dict, repr=False)
    _failed_at: dict[str, float] = field(default_factory=dict, repr=False)
    _last_error: LlmError | None = field(default=None, repr=False)

    def fresh(self, provider: str, *, refresh: bool = False) -> list[ModelInfo] | None:
        """Return a copy of the cached listing while it is younger than the TTL."""
        if refresh:
            return None
        entry = self._entries.get(provider)
        if entry is None:
            return None
        if self.ttl_s <= 0 or (self.clock() - entry.fetched_at) >= self.ttl_s:
            return None
        return list(entry.models)

    def last_good(self, provider: str) -> list[ModelInfo] | None:
        """Return the last successful listing regardless of age (None if never fetched)."""
        entry = self._entries.get(provider)
        if entry is None:
            return None
        return list(entry.models)

    def store(self, provider: str, models: list[ModelInfo]) -> None:
        """Record a successful listing and clear the provider's error."""
        self._entries[provider] = _CatalogEntry(models=list(models), fetched_at=self.clock())
        self._errors.pop(provider, None)
        self._failed_at.pop(provider, None)

    def store_error(self, provider: str, error: LlmError) -> None:
        """Record a failed refresh, keeping the last good listing usable.

        The fetched_at stamp is refreshed so a down gateway is retried at most once per
        TTL instead of on every UI refresh.
        """
        now = self.clock()
        self._errors[provider] = error
        self._failed_at[provider] = now
        self._last_error = error
        entry = self._entries.get(provider)
        if entry is not None:
            entry.fetched_at = now

    def error_for(self, provider: str) -> LlmError | None:
        """Last error recorded for one provider, if its latest refresh failed."""
        return self._errors.get(provider)

    def failure_fresh(self, provider: str) -> bool:
        """True while a failure is recent enough that discovery should not be retried.

        This backs off providers that have no last-known-good listing yet; with one, the
        error handler bumps the entry timestamp instead so fresh() serves it.
        """
        failed_at = self._failed_at.get(provider)
        if failed_at is None:
            return False
        return self.ttl_s > 0 and (self.clock() - failed_at) < self.ttl_s

    @property
    def last_error(self) -> LlmError | None:
        """Most recent failure across all providers (for diagnostics/inspect)."""
        return self._last_error

    def clear(self, provider: str | None = None) -> None:
        """Forget cached listings and errors for one provider, or all of them."""
        if provider is None:
            self._entries.clear()
            self._errors.clear()
            self._failed_at.clear()
            self._last_error = None
            return
        self._entries.pop(provider, None)
        self._errors.pop(provider, None)
        self._failed_at.pop(provider, None)


__all__ = [
    "ANTHROPIC_MODEL_LIMIT",
    "DEFAULT_ANTHROPIC_VERSION",
    "DEFAULT_CATALOG_TIMEOUT_S",
    "DEFAULT_CATALOG_TTL_S",
    "MAX_LISTING_BYTES",
    "OPENAI_LISTING_SUFFIX",
    "CatalogCache",
    "anthropic_listing_url",
    "fetch_listing",
    "openai_listing_url",
]
