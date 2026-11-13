"""Model listing discovery: URL normalization, HTTP fetching and the TTL cache."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from deeppyro.llm.catalog import ModelInfo
from deeppyro.llm.discovery import (
    ANTHROPIC_MODEL_LIMIT,
    DEFAULT_CATALOG_TIMEOUT_S,
    DEFAULT_CATALOG_TTL_S,
    MAX_LISTING_BYTES,
    CatalogCache,
    anthropic_listing_url,
    fetch_listing,
    openai_listing_url,
)
from deeppyro.llm.errors import (
    CODE_AUTH,
    CODE_PROTOCOL,
    CODE_RATE_LIMIT,
    CODE_SERVER,
    CODE_TIMEOUT,
    CODE_TRANSPORT,
    LlmHttpError,
    LlmRequestError,
)

URL = "http://127.0.0.1:8000/v1/models"
PAYLOAD = {"object": "list", "data": [{"id": "deepseek-chat"}, {"id": "local-9b"}]}


# --------------------------------------------------------------------------
# URL normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1/models"),
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000/v1/models"),
        ("https://api.deepseek.com", "https://api.deepseek.com/models"),
        (
            "https://api.deepseek.com/v1/chat/completions",
            "https://api.deepseek.com/v1/models",
        ),
        ("http://127.0.0.1:8000/completions", "http://127.0.0.1:8000/models"),
        ("http://127.0.0.1:8000/v1/models", "http://127.0.0.1:8000/v1/models"),
    ],
)
def test_openai_listing_url(base: str, expected: str) -> None:
    assert openai_listing_url(base) == expected


@pytest.mark.parametrize(
    "base",
    [
        "https://api.anthropic.com",
        "https://api.anthropic.com/",
        "https://api.anthropic.com/v1",
        "https://api.anthropic.com/v1/",
        "https://api.anthropic.com/v1/messages",
        "https://api.anthropic.com/messages",
        "https://api.anthropic.com/v1/models",
    ],
)
def test_anthropic_listing_url(base: str) -> None:
    assert anthropic_listing_url(base) == (
        f"https://api.anthropic.com/v1/models?limit={ANTHROPIC_MODEL_LIMIT}"
    )


def test_discovery_constants() -> None:
    assert DEFAULT_CATALOG_TIMEOUT_S == 5.0
    assert DEFAULT_CATALOG_TTL_S == 600.0
    assert MAX_LISTING_BYTES == 4 * 1024 * 1024
    assert ANTHROPIC_MODEL_LIMIT == 1000


# --------------------------------------------------------------------------
# fetch_listing
# --------------------------------------------------------------------------


@respx.mock
async def test_fetch_listing_returns_json_and_sends_headers() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=PAYLOAD))
    async with httpx.AsyncClient() as client:
        payload = await fetch_listing(client, URL, headers={"authorization": "Bearer k"}, provider="local")
    assert payload == PAYLOAD
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer k"
    assert request.headers["accept"] == "*/*"


@respx.mock
async def test_fetch_listing_maps_http_errors() -> None:
    route = respx.get(URL)
    async with httpx.AsyncClient() as client:
        route.mock(return_value=httpx.Response(429, headers={"retry-after": "7"}))
        with pytest.raises(LlmHttpError) as rate_limited:
            await fetch_listing(client, URL, provider="local")
        assert rate_limited.value.status == 429
        assert rate_limited.value.code == CODE_RATE_LIMIT
        assert rate_limited.value.retryable is True
        assert rate_limited.value.retry_after == 7.0

        route.mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
        with pytest.raises(LlmHttpError) as auth:
            await fetch_listing(client, URL, provider="local")
        assert auth.value.code == CODE_AUTH
        assert auth.value.retryable is False
        assert "bad key" in str(auth.value)

        route.mock(return_value=httpx.Response(500, text="upstream down"))
        with pytest.raises(LlmHttpError) as server:
            await fetch_listing(client, URL, provider="local")
        assert server.value.code == CODE_SERVER
        assert server.value.retryable is True


@respx.mock
async def test_fetch_listing_reports_redirects_instead_of_following() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            302, headers={"location": "https://login.example/", "content-length": "0"}
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(LlmHttpError) as excinfo:
            await fetch_listing(client, URL, provider="local")
    assert excinfo.value.status == 302
    assert excinfo.value.retryable is False


@respx.mock
async def test_fetch_listing_maps_timeout_and_transport_errors() -> None:
    route = respx.get(URL)
    async with httpx.AsyncClient() as client:
        route.mock(side_effect=httpx.ConnectTimeout("slow"))
        with pytest.raises(LlmRequestError) as timeout:
            await fetch_listing(client, URL, provider="local")
        assert timeout.value.code == CODE_TIMEOUT
        assert timeout.value.retryable is True
        assert timeout.value.status is None

        route.mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(LlmRequestError) as transport:
            await fetch_listing(client, URL, provider="local")
        assert transport.value.code == CODE_TRANSPORT
        assert transport.value.retryable is True


@respx.mock
async def test_fetch_listing_rejects_non_json() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>login</html>"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(LlmRequestError) as excinfo:
            await fetch_listing(client, URL, provider="local")
    assert excinfo.value.code == CODE_PROTOCOL
    assert "login" in str(excinfo.value)


@respx.mock
async def test_fetch_listing_rejects_oversized_payloads() -> None:
    body = b'{"data": [' + b" " * (MAX_LISTING_BYTES + 1)
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "application/json"})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(LlmRequestError) as excinfo:
            await fetch_listing(client, URL, provider="local")
    assert excinfo.value.code == CODE_PROTOCOL
    assert "limit" in str(excinfo.value)


@respx.mock
async def test_fetch_listing_rejects_oversized_content_length() -> None:
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=b"{}",
            headers={"content-length": str(MAX_LISTING_BYTES + 1)},
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(LlmRequestError) as excinfo:
            await fetch_listing(client, URL, provider="local")
    assert excinfo.value.code == CODE_PROTOCOL


@respx.mock
async def test_fetch_listing_defaults_timeout_parameter() -> None:
    # Smoke: a tiny body still works and the call path is exercised with default timeout.
    respx.get(URL).mock(return_value=httpx.Response(200, json={"data": []}))
    async with httpx.AsyncClient() as client:
        payload = await fetch_listing(client, URL, provider="local")
    assert payload == {"data": []}


# --------------------------------------------------------------------------
# CatalogCache
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _models(*ids: str) -> list[ModelInfo]:
    return [ModelInfo(provider="p", id=model_id) for model_id in ids]


def test_cache_fresh_returns_copy_within_ttl() -> None:
    clock = FakeClock()
    cache = CatalogCache(clock=clock)
    cache.store("p", _models("a", "b"))
    fresh = cache.fresh("p")
    assert fresh is not None
    assert [m.id for m in fresh] == ["a", "b"]
    fresh.pop()  # mutating the returned list must not corrupt the cache
    assert [m.id for m in cache.fresh("p") or []] == ["a", "b"]


def test_cache_expires_after_ttl_but_keeps_last_good() -> None:
    clock = FakeClock()
    cache = CatalogCache(clock=clock)
    cache.store("p", _models("a"))
    clock.now += DEFAULT_CATALOG_TTL_S
    assert cache.fresh("p") is None
    assert cache.last_good("p") is not None


def test_cache_refresh_flag_bypasses_fresh() -> None:
    clock = FakeClock()
    cache = CatalogCache(clock=clock)
    cache.store("p", _models("a"))
    assert cache.fresh("p", refresh=True) is None


def test_cache_zero_ttl_is_never_fresh() -> None:
    clock = FakeClock()
    cache = CatalogCache(ttl_s=0, clock=clock)
    cache.store("p", _models("a"))
    assert cache.fresh("p") is None
    assert cache.last_good("p") is not None


def test_cache_store_error_keeps_listing_and_backs_off() -> None:
    clock = FakeClock()
    cache = CatalogCache(clock=clock)
    cache.store("p", _models("a"))
    clock.now += DEFAULT_CATALOG_TTL_S  # entry is now stale
    error = LlmRequestError("boom", provider="p", retryable=True, code=CODE_SERVER)
    cache.store_error("p", error)
    assert cache.error_for("p") is error
    assert cache.last_error is error
    # failure refreshed fetched_at, so the (now last-known-good) listing is fresh again
    assert cache.fresh("p") is not None
    # a later success clears the error
    cache.store("p", _models("b"))
    assert cache.error_for("p") is None
    assert [m.id for m in cache.fresh("p") or []] == ["b"]


def test_cache_store_error_for_unknown_provider() -> None:
    cache = CatalogCache(clock=FakeClock())
    error = LlmRequestError("boom", code=CODE_TRANSPORT, retryable=True)
    cache.store_error("p", error)
    assert cache.last_good("p") is None
    assert cache.last_error is error


def test_cache_clear_one_and_all() -> None:
    clock = FakeClock()
    cache = CatalogCache(clock=clock)
    cache.store("a", _models("a1"))
    cache.store("b", _models("b1"))
    cache.clear("a")
    assert cache.last_good("a") is None
    assert cache.last_good("b") is not None
    cache.store_error("b", LlmRequestError("x", code=CODE_SERVER))
    cache.clear()
    assert cache.last_good("b") is None
    assert cache.last_error is None
    assert json.dumps(cache.last_good("a"))  # None serializes; cache remains inert
