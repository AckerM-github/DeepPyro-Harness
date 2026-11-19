"""The browser-facing auth boundary: one token hop, then a cookie.

These tests drive the middleware as a plain ASGI app so they do not need a
running server. Every assertion is about the edge contract the browser and the
CLI both rely on: the token never stays in the URL, the cookie is HttpOnly and
strict, and a cross-site page cannot get a session even with a stolen token.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from deeppyro.web.ui.auth import COOKIE_NAME, LoopbackAuthMiddleware, build_cookie

TOKEN = "test-token-0123456789"
PORT = 8765


class _Downstream:
    """Records the scopes that pass the middleware."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.calls.append(scope)
        if scope.get("type") == "websocket":
            await send({"type": "websocket.accept"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})


def _scope(
    path: str = "/",
    *,
    query: str = "",
    cookie: str | None = None,
    authorization: str | None = None,
    host: str | None = None,
    origin: str | None = None,
    fetch_site: str | None = None,
    kind: str = "http",
) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = [
        (b"host", (host if host is not None else f"127.0.0.1:{PORT}").encode())
    ]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    return {
        "type": kind,
        "path": path,
        "query_string": query.encode(),
        "headers": headers,
        "scheme": "http",
        "server": ("127.0.0.1", PORT),
        "client": ("127.0.0.1", 50000),
    }


async def _exchange(
    scope: dict[str, Any],
    downstream: _Downstream | None = None,
) -> tuple[list[dict[str, Any]], _Downstream]:
    app = downstream or _Downstream()
    middleware = LoopbackAuthMiddleware(app, token=TOKEN, port=lambda: PORT)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    await middleware(scope, receive, send)
    return sent, app


def _header(message: dict[str, Any], name: str) -> str | None:
    for key, value in message.get("headers", ()):
        if key.decode().lower() == name:
            return value.decode()
    return None


def _status(sent: list[dict[str, Any]]) -> int:
    return int(sent[0]["status"])


@pytest.mark.asyncio
async def test_first_page_hit_with_token_sets_cookie_and_redirects_clean() -> None:
    sent, downstream = await _exchange(_scope("/c/native", query=f"token={TOKEN}&x=1"))
    assert not downstream.calls
    assert _status(sent) == 303
    assert _header(sent[0], "location") == "/c/native?x=1"
    cookie = _header(sent[0], "set-cookie")
    assert cookie == build_cookie(TOKEN)
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert TOKEN in cookie
    assert "token=" not in cookie


@pytest.mark.asyncio
async def test_cookie_reaches_the_page_after_the_first_hop() -> None:
    sent, downstream = await _exchange(_scope("/settings", cookie=f"{COOKIE_NAME}={TOKEN}"))
    assert _status(sent) == 200
    assert downstream.calls[0]["path"] == "/settings"


@pytest.mark.asyncio
async def test_missing_token_is_a_json_401() -> None:
    sent, downstream = await _exchange(_scope("/"))
    assert not downstream.calls
    assert _status(sent) == 401
    assert _header(sent[0], "content-type").startswith("application/json")
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_wrong_cookie_is_rejected() -> None:
    sent, downstream = await _exchange(_scope("/", cookie=f"{COOKIE_NAME}=not-the-token"))
    assert not downstream.calls
    assert _status(sent) == 401


@pytest.mark.asyncio
async def test_api_accepts_a_bearer_without_redirecting() -> None:
    sent, downstream = await _exchange(_scope("/api/ui/bootstrap", authorization=f"Bearer {TOKEN}"))
    assert _status(sent) == 200
    assert downstream.calls[0]["path"] == "/api/ui/bootstrap"


@pytest.mark.asyncio
async def test_api_accepts_a_query_token_for_cli_clients() -> None:
    sent, downstream = await _exchange(_scope("/api/health", query=f"token={TOKEN}"))
    assert _status(sent) == 200
    assert downstream.calls


@pytest.mark.asyncio
async def test_websocket_with_the_cookie_is_accepted() -> None:
    sent, downstream = await _exchange(
        _scope("/_nicegui/ws", cookie=f"{COOKIE_NAME}={TOKEN}", kind="websocket")
    )
    assert sent[0]["type"] == "websocket.accept"
    assert len(downstream.calls) == 1


@pytest.mark.asyncio
async def test_websocket_without_a_token_is_closed() -> None:
    sent, downstream = await _exchange(_scope("/_nicegui/ws", kind="websocket"))
    assert sent[0] == {"type": "websocket.close", "code": 1008}
    assert not downstream.calls


@pytest.mark.asyncio
async def test_cross_site_websocket_is_closed() -> None:
    sent, downstream = await _exchange(
        _scope(
            "/_nicegui/ws",
            cookie=f"{COOKIE_NAME}={TOKEN}",
            origin="http://evil.example",
            kind="websocket",
        )
    )
    assert sent[0] == {"type": "websocket.close", "code": 1008}
    assert not downstream.calls


@pytest.mark.asyncio
async def test_brand_asset_with_a_bearer_is_served_without_a_redirect() -> None:
    sent, downstream = await _exchange(_scope("/brand/style.css", authorization=f"Bearer {TOKEN}"))
    assert _status(sent) == 200
    assert downstream.calls[0]["path"] == "/brand/style.css"


@pytest.mark.asyncio
async def test_cross_site_origin_is_forbidden_even_with_a_valid_cookie() -> None:
    sent, downstream = await _exchange(
        _scope(
            "/",
            cookie=f"{COOKIE_NAME}={TOKEN}",
            origin="https://evil.example",
            fetch_site="cross-site",
        )
    )
    assert not downstream.calls
    assert _status(sent) == 403
    assert json.loads(sent[1]["body"])["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_wrong_host_is_forbidden() -> None:
    sent, downstream = await _exchange(_scope("/", cookie=f"{COOKIE_NAME}={TOKEN}", host="evil.example"))
    assert not downstream.calls
    assert _status(sent) == 403


@pytest.mark.asyncio
async def test_websocket_without_a_token_closes_1008() -> None:
    sent, downstream = await _exchange(_scope("/api/ui/socket", kind="websocket"))
    assert not downstream.calls
    assert sent == [{"type": "websocket.close", "code": 1008}]


@pytest.mark.asyncio
async def test_websocket_with_a_cookie_reaches_the_app() -> None:
    sent, downstream = await _exchange(
        _scope("/api/ui/socket", kind="websocket", cookie=f"{COOKIE_NAME}={TOKEN}")
    )
    assert sent == [{"type": "websocket.accept"}]
    assert downstream.calls
