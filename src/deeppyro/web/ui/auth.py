"""Cookie-and-token authentication for the loopback web UI.

The browser is a different untrusted edge from the JSON API. A page request
carries the per-process bearer token once in the query string; the ASGI
middleware validates it, swaps it for a SameSite=Strict HttpOnly cookie, and
redirects to the clean URL. Every later page, asset, API and WebSocket request
is authenticated by that cookie (or an Authorization header for tests and CLI
clients), so the token never sits in the address bar after the first hop. The
cookie name is scoped to the bound port because browsers scope cookies by host
only: a generic name would let any other loopback service read or overwrite it.

This module owns the ASGI boundary only. It does not know about sessions,
the shell or the kernel; it composes the checks in deeppyro.web.security so
there is one definition of loopback, Host, Origin and token equality.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlunsplit

from deeppyro.web.security import (
    JSON_CONTENT_TYPE,
    LOCALHOST,
    SECURITY_HEADERS,
    host_allowed,
    origin_allowed,
    token_matches,
)
from deeppyro.web.wire import MAX_ERROR_CHARS

#: Prefix of the HttpOnly cookie the browser carries after the first tokenized
#: page request. The concrete name is scoped to the bound port by cookie_name():
#: browsers scope cookies by host only, so a generic name would let any other
#: loopback service (or another DeepPyro instance) read and overwrite it.
COOKIE_PREFIX = "dpyro_web"

#: The token query parameter accepted on the first page request.
TOKEN_QUERY = "token"

#: Content-Security-Policy for the React/Vite SPA. The Vite build ships external
#: scripts and one external stylesheet, streams SSE from the same origin and may
#: use workers for the bundled editors, so those grants are explicit. React's
#: inline style props are applied through the CSSOM, which CSP does not govern,
#: and the built shell has no inline style elements or style attributes, so
#: style-src needs no 'unsafe-inline'. There is no eval and no CDN: everything
#: else is same-origin.
UI_CSP = (
    "default-src 'none'; script-src 'self'; "
    "style-src 'self'; connect-src 'self' ws: wss:; "
    "img-src 'self' data: blob:; font-src 'self' data:; "
    "worker-src 'self' blob:; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)

#: Dotted cookie attributes safe to emit for a loopback-only host.
_COOKIE_DIRECTIVES = ("path=/", "httponly", "samesite=strict")


def cookie_name(port: int) -> str:
    """Name the session cookie for the one port this server is bound to.

    Cookies are host-scoped, not port-scoped: every service on 127.0.0.1 shares
    one cookie jar for the host. Suffixing the bound port keeps this session
    unreadable and unoverwritable from any other localhost port.
    """
    return f"{COOKIE_PREFIX}_{int(port)}"


class LoopbackAuthMiddleware:
    """Authenticate HTTP and WebSocket scopes with the process token or cookie."""

    def __init__(
        self,
        app: Any,
        *,
        token: str,
        port: Callable[[], int],
        bind_host: str = LOCALHOST,
    ) -> None:
        self._app = app
        self._token = token
        self._port = port
        self._bind_host = bind_host

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        headers = _headers(scope)
        port = int(self._port())
        if not self._same_origin(headers, port=port):
            await self._reject(scope, receive, send, 403, "unexpected Host or Origin")
            return
        presented = _presented_token(headers, scope.get("query_string", b""))
        cookie_ok = token_matches(self._token, _cookie_token(headers, cookie_name(port)))
        header_ok = token_matches(self._token, presented)
        if not (cookie_ok or header_ok):
            await self._reject(scope, receive, send, 401, "missing or invalid web token")
            return
        if kind == "websocket":
            await self._app(scope, receive, send)
            return
        path = str(scope.get("path", "/") or "/")
        query = scope.get("query_string", b"")
        if _is_page(path) and (not cookie_ok or _query_has_token(query)):
            # First browser hit (a valid query token without the cookie yet), or
            # a page URL that still carries the token: always redirect to the
            # clean URL so the token never stays in the address bar or history.
            # APIs, assets and WebSockets are never redirected, so a HEAD or an
            # asset preload cannot turn into a redirect loop.
            await self._redirect(scope, receive, send, path=path, query=query, port=port)
            return
        await self._app(scope, receive, send)

    def _same_origin(self, headers: Mapping[str, str], *, port: int) -> bool:
        host = headers.get("host", "")
        if host and not host_allowed(host, port=port):
            return False
        origin = headers.get("origin")
        if origin is not None and not origin_allowed(origin, port=port):
            return False
        fetch_site = headers.get("sec-fetch-site")
        return fetch_site is None or fetch_site.lower() in {"same-origin", "none"}

    async def _reject(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        status: int,
        message: str,
    ) -> None:
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps(
            {
                "error": {
                    "code": "unauthorized" if status == 401 else "forbidden",
                    "message": message[:MAX_ERROR_CHARS],
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", JSON_CONTENT_TYPE.encode()),
                    (b"content-length", str(len(body)).encode()),
                    *[(name.encode(), value.encode()) for name, value in SECURITY_HEADERS],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _redirect(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        path: str,
        query: bytes,
        port: int,
    ) -> None:
        remaining = [
            (key, value)
            for key, value in parse_qsl(query.decode("latin-1"), keep_blank_values=True)
            if key != TOKEN_QUERY
        ]
        location = urlunsplit(("", "", path, urlencode(remaining), ""))
        headers = [
            (b"location", location.encode("latin-1")),
            (b"set-cookie", build_cookie(self._token, port=port).encode("latin-1")),
            (b"content-length", b"0"),
            *[(name.encode(), value.encode()) for name, value in SECURITY_HEADERS],
        ]
        await send({"type": "http.response.start", "status": 303, "headers": headers})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def _headers(scope: Mapping[str, Any]) -> dict[str, str]:
    pairs = scope.get("headers", ())
    return {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in pairs}


def _cookie_token(headers: Mapping[str, str], name: str) -> str | None:
    """Read one named cookie from the Cookie header."""
    raw = headers.get("cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


def _presented_token(headers: Mapping[str, str], query: bytes) -> str | None:
    header = headers.get("authorization", "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    for key, value in parse_qsl(query.decode("latin-1"), keep_blank_values=True):
        if key == TOKEN_QUERY:
            return value or None
    return None


def _query_has_token(query: bytes) -> bool:
    """Whether the raw query string carries a token key at all, even empty."""
    return any(key == TOKEN_QUERY for key, _ in parse_qsl(query.decode("latin-1"), keep_blank_values=True))


#: Exact paths that are served directly, never as an HTML page.
_NON_PAGE_PATHS = frozenset({"/api", "/assets", "/brand", "/favicon.ico"})
#: Prefixes that are served directly; a token must never trigger a redirect.
_NON_PAGE_PREFIXES = ("/api/", "/assets/", "/brand/")


def _is_page(path: str) -> bool:
    """Whether a token-bearing request is an HTML navigation worth a redirect.

    The JSON API and the static bundles (the hashed SPA build, the brand assets
    and the favicon) are served directly so a HEAD or an asset preload never
    turns into a redirect loop. `/ui/` is the legacy page prefix and, like
    every other non-API path, falls through to the SPA shell.
    """
    if path in _NON_PAGE_PATHS:
        return False
    return not path.startswith(_NON_PAGE_PREFIXES)


def build_cookie(token: str, *, port: int) -> str:
    """Build the Set-Cookie value for one validated token on one bound port."""
    return f"{cookie_name(port)}={token}; " + "; ".join(_COOKIE_DIRECTIVES)


__all__ = [
    "COOKIE_PREFIX",
    "TOKEN_QUERY",
    "UI_CSP",
    "LoopbackAuthMiddleware",
    "build_cookie",
    "cookie_name",
]
