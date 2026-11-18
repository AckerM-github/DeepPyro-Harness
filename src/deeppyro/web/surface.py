"""The web surface seam: routes registered as plugin contributions.

Principle 1 (everything is a plugin): the built-in HTTP/JSON/SSE API and the
static assets are just two WebSurface implementations registered in a route
registry owned by WebHostPlugin. Another plugin can inject the web service and
register its own route or UI panel without editing the server; the kernel has no
route or panel slot today, so this registry is the local UI-slot equivalent.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from deeppyro.web import assets
from deeppyro.web.wire import (
    BadRequest,
    CreateSessionRequest,
    JsonValue,
    Method,
    MethodNotAllowed,
    NotFound,
    SendMessageRequest,
    ServiceUnavailable,
    SSEStream,
    WebRequest,
    WebResponse,
    json_response,
    parse_session_id,
    text_response,
)

#: A route handler: receives the web service (structurally a WebService) and the
#: parsed request, and returns one response or an SSE stream.
Handler = Callable[[Any, WebRequest], Awaitable[WebResponse | SSEStream]]


@dataclass(frozen=True, slots=True)
class Route:
    """One method + path pattern bound to an async handler."""

    method: Method
    pattern: str
    handler: Handler
    name: str = ""

    @property
    def segments(self) -> tuple[str, ...]:
        """Pattern segments, without empty pieces; {name} segments are params."""
        return tuple(segment for segment in self.pattern.split("/") if segment)

    def match(self, path: str) -> dict[str, str] | None:
        """Extract path params when the pattern matches, else None."""
        target = tuple(segment for segment in path.split("/") if segment)
        segments = self.segments
        if len(target) != len(segments):
            return None
        params: dict[str, str] = {}
        for actual, wanted in zip(target, segments, strict=True):
            if wanted.startswith("{") and wanted.endswith("}"):
                params[wanted[1:-1]] = actual
                continue
            if actual != wanted:
                return None
        return params


def _normalize_path(path: str) -> str:
    """Collapse duplicate and trailing slashes for route matching."""
    if not path.startswith("/"):
        raise BadRequest("request target must be an absolute path")
    normalized = "/" + "/".join(segment for segment in path.split("/") if segment)
    return normalized


@runtime_checkable
class WebSurface(Protocol):
    """A named group of routes contributed to the host."""

    @property
    def name(self) -> str:
        """Stable surface name, e.g. api or assets."""
        ...

    def routes(self) -> Iterable[Route]:
        """Routes this surface contributes."""
        ...


class RouteRegistry:
    """Ordered collection of routes with duplicate and method guards."""

    def __init__(self) -> None:
        self._routes: list[Route] = []
        self._keys: set[tuple[Method, str]] = set()

    def add(self, *routes: Route) -> None:
        """Register routes; a duplicate (method, pattern) is a plugin bug."""
        for route in routes:
            key = (route.method, _normalize_path(route.pattern))
            if key in self._keys:
                raise ValueError(f"duplicate web route: {route.method} {route.pattern}")
            self._keys.add(key)
            self._routes.append(route)

    def add_surface(self, surface: WebSurface) -> None:
        """Register every route of one surface."""
        self.add(*surface.routes())

    def route(
        self,
        method: Method,
        pattern: str,
        *,
        name: str = "",
    ) -> Callable[[Handler], Handler]:
        """Decorator form used by third-party surfaces."""

        def register(handler: Handler) -> Handler:
            self.add(Route(method=method, pattern=pattern, handler=handler, name=name))
            return handler

        return register

    def routes(self) -> tuple[Route, ...]:
        """Every registered route in registration order."""
        return tuple(self._routes)

    def allowed(self, path: str) -> tuple[str, ...]:
        """Methods the path accepts, for 405 Allow headers and OPTIONS."""
        methods: set[str] = set()
        for route in self._routes:
            if route.match(_normalize_path(path)) is not None:
                methods.add(str(route.method))
                if route.method is Method.GET:
                    methods.add(str(Method.HEAD))
        methods.update({"OPTIONS"})
        return tuple(sorted(methods))

    def match(self, method: Method, path: str) -> tuple[Route, dict[str, str]]:
        """Resolve one request or raise NotFound/MethodNotAllowed."""
        normalized = _normalize_path(path)
        allowed: set[Method] = set()
        for route in self._routes:
            params = route.match(normalized)
            if params is None:
                continue
            if route.method is method or (method is Method.HEAD and route.method is Method.GET):
                return route, params
            allowed.add(route.method)
        if allowed:
            names = {str(item) for item in allowed}
            if Method.GET in allowed:
                names.add(str(Method.HEAD))
            names.add(str(Method.OPTIONS))
            raise MethodNotAllowed(tuple(sorted(names)))
        raise NotFound(f"no route for {normalized}")


class WebService(Protocol):
    """The host service the built-in API routes talk to.

    WebSessionHost implements this protocol structurally; tests may pass a fake
    that returns plain snake_case dictionaries.
    """

    async def health(self) -> dict[str, JsonValue]:
        """Liveness and identity document."""
        ...

    async def list_sessions(self) -> dict[str, JsonValue]:
        """Session catalogue, newest first."""
        ...

    async def create_session(self, request: CreateSessionRequest) -> dict[str, JsonValue]:
        """Create and attach a new native session."""
        ...

    async def resume_session(self, session_id: str) -> dict[str, JsonValue]:
        """Attach an existing session by id."""
        ...

    async def send_message(self, session_id: str, request: SendMessageRequest) -> dict[str, JsonValue]:
        """Queue or start one user turn."""
        ...

    async def interrupt(self, session_id: str) -> dict[str, JsonValue]:
        """Interrupt the active turn through the native agent host."""
        ...

    async def transcript(self, session_id: str) -> dict[str, JsonValue]:
        """Replay the session transcript from native facts."""
        ...

    async def status(self, session_id: str) -> dict[str, JsonValue]:
        """Model, preset, usage and context status."""
        ...

    def sse(self, session_id: str, *, after: int = 0) -> Any:
        """Async iterator of encoded SSE frames for one session."""
        ...


def _require_service(service: object | None) -> Any:
    if service is None:
        raise ServiceUnavailable("the web session host is not running")
    return service


def _session_id(request: WebRequest) -> str:
    return parse_session_id(request.path_params.get("session_id", ""))


def _after(request: WebRequest) -> int:
    raw = request.header("last-event-id") or request.query.get("after")
    if raw is None or raw == "":
        return 0
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError) as exc:
        raise BadRequest("invalid SSE replay position") from exc


class ApiSurface:
    """The JSON command API and the session event stream."""

    @property
    def name(self) -> str:
        """Surface name."""
        return "api"

    def routes(self) -> tuple[Route, ...]:
        """All API routes; handlers receive the WebService instance."""
        return (
            Route(Method.GET, "/api/health", self.health, "web.health"),
            Route(Method.GET, "/api/sessions", self.list_sessions, "web.sessions"),
            Route(Method.POST, "/api/sessions", self.create_session, "web.create"),
            Route(Method.GET, "/api/sessions/{session_id}", self.status, "web.status"),
            Route(Method.POST, "/api/sessions/{session_id}/resume", self.resume, "web.resume"),
            Route(Method.GET, "/api/sessions/{session_id}/transcript", self.transcript, "web.transcript"),
            Route(Method.POST, "/api/sessions/{session_id}/messages", self.send_message, "web.send"),
            Route(Method.POST, "/api/sessions/{session_id}/interrupt", self.interrupt, "web.interrupt"),
            Route(Method.GET, "/api/sessions/{session_id}/events", self.events, "web.events"),
        )

    async def health(self, service: object | None, request: WebRequest) -> WebResponse:
        """Liveness and identity."""
        return json_response(await _require_service(service).health())

    async def list_sessions(self, service: object | None, request: WebRequest) -> WebResponse:
        """Session catalogue."""
        return json_response(await _require_service(service).list_sessions())

    async def create_session(self, service: object | None, request: WebRequest) -> WebResponse:
        """Create a session from a validated JSON body."""
        payload = CreateSessionRequest.from_payload(request.json_object())
        result = await _require_service(service).create_session(payload)
        return json_response(result, status=201)

    async def resume(self, service: object | None, request: WebRequest) -> WebResponse:
        """Attach an existing session."""
        return json_response(await _require_service(service).resume_session(_session_id(request)))

    async def status(self, service: object | None, request: WebRequest) -> WebResponse:
        """Status of one attached session."""
        return json_response(await _require_service(service).status(_session_id(request)))

    async def transcript(self, service: object | None, request: WebRequest) -> WebResponse:
        """Transcript replay from facts."""
        return json_response(await _require_service(service).transcript(_session_id(request)))

    async def send_message(self, service: object | None, request: WebRequest) -> WebResponse:
        """Queue or start one user turn."""
        payload = SendMessageRequest.from_payload(request.json_object())
        result = await _require_service(service).send_message(_session_id(request), payload)
        return json_response(result, status=202)

    async def interrupt(self, service: object | None, request: WebRequest) -> WebResponse:
        """Interrupt the active turn."""
        return json_response(await _require_service(service).interrupt(_session_id(request)))

    async def events(self, service: object | None, request: WebRequest) -> WebResponse | SSEStream:
        """Server-sent events for one session, replayable by Last-Event-ID."""
        probe = _require_service(service)
        source = probe.sse(_session_id(request), after=_after(request))
        return SSEStream(source=source)


class AssetSurface:
    """The single-page UI and its two static assets."""

    def __init__(self, token_provider: Callable[[], str]) -> None:
        self._token_provider = token_provider

    @property
    def name(self) -> str:
        """Surface name."""
        return "assets"

    def routes(self) -> tuple[Route, ...]:
        """Index page, stylesheet and script."""
        return (
            Route(Method.GET, "/", self.index, "web.index"),
            Route(Method.GET, "/assets/app.css", self.stylesheet, "web.css"),
            Route(Method.GET, "/assets/app.js", self.script, "web.js"),
        )

    async def index(self, service: object | None, request: WebRequest) -> WebResponse:
        """The page shell with the token templated into its asset URLs."""
        return text_response(
            assets.index_html(self._token_provider()),
            content_type="text/html; charset=utf-8",
        )

    async def stylesheet(self, service: object | None, request: WebRequest) -> WebResponse:
        """The stylesheet."""
        return text_response(assets.APP_CSS, content_type="text/css; charset=utf-8")

    async def script(self, service: object | None, request: WebRequest) -> WebResponse:
        """The browser application."""
        return text_response(assets.APP_JS, content_type="text/javascript; charset=utf-8")


def default_surfaces(token_provider: Callable[[], str]) -> tuple[WebSurface, ...]:
    """The surfaces a host serves out of the box."""
    return (AssetSurface(token_provider), ApiSurface())


__all__ = [
    "ApiSurface",
    "AssetSurface",
    "Handler",
    "Route",
    "RouteRegistry",
    "WebService",
    "WebSurface",
    "default_surfaces",
]
