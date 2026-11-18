"""Loopback-only security helpers for the DeepPyro web host.

The web host is a local surface of the plugin kernel, not a network service:
it binds IPv4 loopback, carries a per-process bearer token in its printed URL,
and validates the Host/Origin pair on every request so a DNS-rebinding page or
a cross-site fetch cannot reach it. This module offers no TLS and no
non-loopback bind: those are deliberate omissions, not configuration.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Mapping

from deeppyro.web.wire import Forbidden, Unauthorized

#: The only address the host ever binds.
LOCALHOST = "127.0.0.1"

#: Host-header host names accepted for a loopback listener.
LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})

#: Query parameter that carries the token for EventSource (no custom headers).
TOKEN_QUERY = "token"

#: Header that carries the token for fetch/JSON commands.
TOKEN_HEADER = "authorization"

#: Bearer scheme used with TOKEN_HEADER.
BEARER_PREFIX = "bearer "

#: The one JSON Content-Type the host produces.
JSON_CONTENT_TYPE = "application/json; charset=utf-8"

#: Headers every response carries. The token never appears in a header.
SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "content-security-policy",
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    ),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("cache-control", "no-store"),
)


def new_token() -> str:
    """Return the per-process bearer token embedded in the printed URL."""
    return secrets.token_urlsafe(32)


def validate_bind_host(host: str) -> str:
    """Validate a requested bind host and return the loopback address to use.

    localhost is accepted as a convenience and normalised to 127.0.0.1; every
    other value is refused outright rather than silently clamped.
    """
    normalized = host.strip().lower()
    if normalized in {"localhost", LOCALHOST}:
        return LOCALHOST
    raise ValueError(f"web host may only bind loopback (127.0.0.1), refused: {host!r}")


def token_matches(expected: str, presented: str | None) -> bool:
    """Constant-time comparison of the presented token against the process token."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


def request_token(headers: Mapping[str, str], query: Mapping[str, str]) -> str | None:
    """Extract a bearer token from an Authorization header or the token query."""
    header = headers.get(TOKEN_HEADER, "").strip()
    if header.lower().startswith(BEARER_PREFIX):
        return header[len(BEARER_PREFIX) :].strip() or None
    return query.get(TOKEN_QUERY)


def require_token(
    headers: Mapping[str, str],
    query: Mapping[str, str],
    expected: str,
) -> None:
    """Raise Unauthorized when the request does not carry the process token."""
    if not token_matches(expected, request_token(headers, query)):
        raise Unauthorized("missing or invalid bearer token")


def validate_request_origin(headers: Mapping[str, str], *, port: int) -> None:
    """Reject requests a browser could only send from another origin.

    The token is the credential; this check is the second lock. It stops DNS
    rebinding (a hostile name resolving to loopback sends its own Host) and
    cross-site browser requests (which send a foreign Origin or a cross-site
    Sec-Fetch-Site value). Requests without Origin are accepted: non-browser
    clients that already hold the token are the intended user of that path.
    """
    host = headers.get("host", "")
    if not host_allowed(host, port=port):
        raise Forbidden(f"unexpected Host header: {host!r}")
    origin = headers.get("origin")
    if origin is not None and not origin_allowed(origin, port=port):
        raise Forbidden(f"unexpected Origin header: {origin!r}")
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site.lower() not in {"same-origin", "none"}:
        raise Forbidden(f"cross-site request refused: Sec-Fetch-Site={fetch_site!r}")


def host_allowed(host: str, *, port: int) -> bool:
    """Whether a Host header names the loopback listener on its bound port."""
    name, parsed_port = _split_host(host)
    if name not in LOOPBACK_HOSTNAMES:
        return False
    return parsed_port is None or parsed_port == port


def origin_allowed(origin: str, *, port: int) -> bool:
    """Whether an Origin header names the loopback listener on its bound port."""
    text = origin.strip()
    if text.lower() == "null" or "://" not in text:
        return False
    scheme, _, remainder = text.partition("://")
    if scheme.lower() != "http":
        return False
    name, parsed_port = _split_host(remainder)
    if name not in LOOPBACK_HOSTNAMES:
        return False
    return parsed_port == port


def _split_host(value: str) -> tuple[str, int | None]:
    text = value.strip().lower()
    if not text:
        return "", None
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return text, None
        name = text[1:end]
        tail = text[end + 1 :]
        if tail.startswith(":") and tail[1:].isdigit():
            return name, int(tail[1:])
        return name, None
    if text.count(":") == 1:
        name, _, port_text = text.partition(":")
        if port_text.isdigit():
            return name, int(port_text)
        return text, None
    return text, None


def sse_headers() -> tuple[tuple[str, str], ...]:
    """Security headers plus the SSE transport headers."""
    return (*SECURITY_HEADERS, ("content-type", "text/event-stream; charset=utf-8"))


__all__ = [
    "BEARER_PREFIX",
    "JSON_CONTENT_TYPE",
    "LOCALHOST",
    "LOOPBACK_HOSTNAMES",
    "SECURITY_HEADERS",
    "TOKEN_HEADER",
    "TOKEN_QUERY",
    "host_allowed",
    "new_token",
    "origin_allowed",
    "request_token",
    "require_token",
    "sse_headers",
    "token_matches",
    "validate_bind_host",
    "validate_request_origin",
]
