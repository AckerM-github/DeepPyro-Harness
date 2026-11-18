"""HTTP host, security and native-session tests for the loopback web UI.

The suite uses a real ephemeral socket on 127.0.0.1 (no external network) and
skips cleanly when loopback TCP is unavailable. Server/route/security tests use
a fake WebService; the last group boots the plugin kernel with a scripted LLM
provider so create/send/stream/interrupt/resume run through a real
NativeAgentHost without touching the network.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from deeppyro.builtin.llm import LlmPlugin
from deeppyro.config import Config, ModelConfig, ProviderConfig
from deeppyro.core.llm import Finish, StopReason, TextDelta, Usage, UsageEvent
from deeppyro.kernel import PluginSpec, boot
from deeppyro.web.events import (
    ErrorEvent,
    FinalEvent,
    SessionEvent,
    StatusEvent,
    StatusPhase,
    StepEvent,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    WebEventType,
    encode_sse_frame,
    to_wire,
)
from deeppyro.web.events import (
    UsageEvent as WebUsageEvent,
)
from deeppyro.web.host import WebHost
from deeppyro.web.journal import EventJournal
from deeppyro.web.plugin import WebHostConfig, WebHostPlugin
from deeppyro.web.security import LOCALHOST, SECURITY_HEADERS, validate_bind_host
from deeppyro.web.server import WebServer
from deeppyro.web.surface import Route, RouteRegistry, default_surfaces
from deeppyro.web.wire import (
    MAX_BODY_BYTES,
    CreateSessionRequest,
    Method,
    SendMessageRequest,
    json_response,
)

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "deeppyro" / "web"
TOKEN = "test-token-0123456789"


def _loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((LOCALHOST, 0))
    except OSError:
        return False
    return True


if not _loopback_available():  # pragma: no cover - environment dependent
    pytest.skip("loopback TCP sockets are unavailable in this environment", allow_module_level=True)


# ---------------------------------------------------------------------------
# fake web service + raw HTTP helpers
# ---------------------------------------------------------------------------


class FakeWebService:
    """A structural WebService used to test routing without the kernel."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.events = [TextEvent(session_id="s-1", text="hi")]

    async def health(self) -> dict[str, Any]:
        return {"status": "ok", "version": "test", "active_session_id": "s-1"}

    async def list_sessions(self) -> dict[str, Any]:
        return {"sessions": [{"session_id": "s-1"}], "active_session_id": "s-1"}

    async def create_session(self, request: CreateSessionRequest) -> dict[str, Any]:
        self.calls.append(("create", request))
        return {"session_id": "s-1", "resumed": False}

    async def resume_session(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("resume", session_id))
        return {"session_id": session_id, "resumed": True}

    async def transcript(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("transcript", session_id))
        return {"session_id": session_id, "entries": [], "readable": True, "issues": []}

    async def status(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("status", session_id))
        return {"session_id": session_id, "active": True}

    async def send_message(self, session_id: str, request: SendMessageRequest) -> dict[str, Any]:
        self.calls.append(("send", session_id, request.text))
        return {"session_id": session_id, "accepted": True, "queued": 0}

    async def interrupt(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("interrupt", session_id))
        return {"session_id": session_id, "interrupted": False, "detail": "nothing to interrupt"}

    async def sse(self, session_id: str, *, after: int = 0):
        for seq, event in enumerate(self.events, start=after + 1):
            yield encode_sse_frame(seq, event)


class _CustomSurface:
    """A surface registered by a hypothetical plugin."""

    name = "custom"

    def routes(self) -> tuple[Route, ...]:
        async def handler(_service: object, _request: object) -> Any:
            return json_response({"ok": True, "surface": self.name})

        return (Route(Method.GET, "/custom", handler, name="custom"),)


@contextlib.asynccontextmanager
async def running_server(
    service: FakeWebService | None = None,
    *,
    token: str = TOKEN,
    registry: RouteRegistry | None = None,
):
    backend = service if service is not None else FakeWebService()
    routes = registry if registry is not None else RouteRegistry()
    if registry is None:
        for surface in default_surfaces(lambda: token):
            routes.add_surface(surface)
    server = WebServer(
        token=token,
        registry=routes,
        service_provider=lambda: backend,
        port=0,
    )
    await server.start()
    try:
        yield server, backend
    finally:
        await server.close()


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    line = await asyncio.wait_for(reader.readline(), 10.0)
    if not line:
        raise AssertionError("connection closed before a status line")
    parts = line.decode("latin-1").split(" ", 2)
    status = int(parts[1])
    headers: dict[str, str] = {}
    while True:
        raw = await asyncio.wait_for(reader.readline(), 10.0)
        if raw in (b"\r\n", b"\n", b""):
            break
        name, _, value = raw.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


async def _read_chunk(reader: asyncio.StreamReader) -> bytes:
    line = await asyncio.wait_for(reader.readline(), 10.0)
    if not line:
        return b""
    size = int(line.split(b";")[0].strip(), 16)
    if size == 0:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.readline(), 5.0)
        return b""
    data = await asyncio.wait_for(reader.readexactly(size), 10.0)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(reader.readexactly(2), 5.0)
    return data


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, str]) -> bytes:
    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        while True:
            chunk = await _read_chunk(reader)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    if "content-length" in headers:
        return await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), 10.0)
    return b""


async def _request(
    server: WebServer,
    method: str,
    target: str,
    *,
    token: str | None = None,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    host: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection(LOCALHOST, server.port)
    host_value = host if host is not None else f"127.0.0.1:{server.port}"
    lines = [f"{method} {target} HTTP/1.1"]
    if host_value:
        lines.append(f"Host: {host_value}")
    extra = dict(headers or {})
    if token is not None:
        extra.setdefault("authorization", f"Bearer {token}")
    if body or method in {"POST", "PUT", "PATCH"}:
        extra.setdefault("content-length", str(len(body)))
        extra.setdefault("content-type", "application/json")
    for name, value in extra.items():
        lines.append(f"{name}: {value}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
    await writer.drain()
    try:
        status, response_headers = await _read_head(reader)
        payload = await _read_body(reader, response_headers)
        return status, response_headers, payload
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _collect_sse(
    server: WebServer,
    target: str,
    *,
    token: str | None = None,
    until: Any = None,
    timeout: float = 20.0,
) -> list[dict[str, Any]]:
    reader, writer = await asyncio.open_connection(LOCALHOST, server.port)
    query = f"?token={token}" if token is not None else ""
    writer.write(
        (
            f"GET {target}{query} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.port}\r\n"
            "Accept: text/event-stream\r\n\r\n"
        ).encode("latin-1")
    )
    await writer.drain()
    events: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        status, headers = await asyncio.wait_for(_read_head(reader), timeout)
        assert status == 200, f"SSE returned {status}"
        assert headers.get("content-type", "").startswith("text/event-stream")
        while loop.time() < deadline:
            chunk = await asyncio.wait_for(_read_chunk(reader), max(0.1, deadline - loop.time()))
            if not chunk:
                break
            for line in chunk.decode("utf-8").splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[len("data: ") :]))
            if until is not None and until(events):
                break
        return events
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# ---------------------------------------------------------------------------
# bind + security
# ---------------------------------------------------------------------------


def test_bind_host_validation_refuses_non_loopback() -> None:
    assert validate_bind_host("127.0.0.1") == LOCALHOST
    assert validate_bind_host("localhost") == LOCALHOST
    for refused in ("0.0.0.0", "::", "192.168.1.10", "example.com", ""):
        with pytest.raises(ValueError, match="loopback"):
            validate_bind_host(refused)


async def test_server_binds_loopback_ephemeral_port() -> None:
    async with running_server() as (server, _service):
        assert server.address[0] == "127.0.0.1"
        assert server.port > 0
        assert server.url == f"http://127.0.0.1:{server.port}/"


async def test_missing_and_wrong_token_are_rejected() -> None:
    async with running_server() as (server, service):
        status, headers, payload = await _request(server, "GET", "/api/health")
        assert status == 401
        assert headers["content-type"].startswith("application/json")
        assert b"unauthorized" in payload
        assert (await _request(server, "GET", "/api/health", token="wrong"))[0] == 401
        assert (await _request(server, "GET", f"/api/health?token={TOKEN}"))[0] == 200
        assert (await _request(server, "GET", "/api/health", token=TOKEN))[0] == 200
        assert service.calls == []


async def test_origin_host_and_fetch_site_are_checked() -> None:
    async with running_server() as (server, _service):
        port = server.port
        status, _headers, _payload = await _request(
            server,
            "GET",
            "/api/health",
            token=TOKEN,
            headers={"origin": f"http://127.0.0.1:{port}"},
        )
        assert status == 200
        status, _headers, _payload = await _request(
            server,
            "GET",
            "/api/health",
            token=TOKEN,
            headers={"origin": "https://evil.example"},
        )
        assert status == 403
        status, _headers, _payload = await _request(
            server,
            "GET",
            "/api/health",
            token=TOKEN,
            headers={"origin": "null"},
        )
        assert status == 403
        status, _headers, _payload = await _request(
            server, "GET", "/api/health", token=TOKEN, host="evil.example"
        )
        assert status == 403
        status, _headers, _payload = await _request(server, "GET", "/api/health", token=TOKEN, host="")
        assert status == 403
        status, _headers, _payload = await _request(
            server,
            "GET",
            "/api/health",
            token=TOKEN,
            headers={"sec-fetch-site": "cross-site"},
        )
        assert status == 403
        status, _headers, _payload = await _request(
            server,
            "GET",
            "/api/health",
            token=TOKEN,
            headers={"sec-fetch-site": "same-origin"},
        )
        assert status == 200


async def test_request_body_limit_is_413() -> None:
    async with running_server() as (server, _service):
        status, _headers, payload = await _request(
            server,
            "POST",
            "/api/sessions",
            token=TOKEN,
            headers={"content-length": str(MAX_BODY_BYTES + 1)},
        )
        assert status == 413
        assert b"payload_too_large" in payload or b"too_large" in payload


async def test_static_assets_and_security_headers() -> None:
    async with running_server() as (server, _service):
        status, headers, payload = await _request(server, "GET", f"/?token={TOKEN}")
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        assert headers["cache-control"] == "no-store"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp
        text = payload.decode("utf-8")
        assert "Localhost only" in text
        assert TOKEN in text
        assert "/assets/app.js" in text
        status, headers, payload = await _request(server, "GET", f"/assets/app.js?token={TOKEN}")
        assert status == 200
        assert "javascript" in headers["content-type"]
        assert b"EventSource" in payload
        status, headers, payload = await _request(server, "GET", f"/assets/app.css?token={TOKEN}")
        assert status == 200
        assert "css" in headers["content-type"]
        for name, _value in SECURITY_HEADERS:
            assert name in headers


async def test_unknown_route_404_and_method_405() -> None:
    async with running_server() as (server, _service):
        status, _headers, payload = await _request(server, "GET", f"/api/nope?token={TOKEN}")
        assert status == 404
        assert b"not_found" in payload
        status, headers, payload = await _request(server, "DELETE", f"/api/health?token={TOKEN}")
        assert status == 405
        assert "GET" in headers["allow"]
        assert b"method_not_allowed" in payload


async def test_invalid_json_payload_is_400() -> None:
    async with running_server() as (server, _service):
        status, _headers, payload = await _request(
            server,
            "POST",
            f"/api/sessions?token={TOKEN}",
            body=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert status == 400
        assert b"bad_request" in payload


async def test_keep_alive_serves_two_requests_on_one_connection() -> None:
    async with running_server() as (server, _service):
        reader, writer = await asyncio.open_connection(LOCALHOST, server.port)
        try:
            for _ in range(2):
                writer.write(
                    (
                        f"GET /api/health?token={TOKEN} HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\n\r\n"
                    ).encode("latin-1")
                )
                await writer.drain()
                status, headers = await _read_head(reader)
                payload = await _read_body(reader, headers)
                assert status == 200
                assert headers.get("connection", "").lower() == "keep-alive"
                assert b'"ok"' in payload
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


async def test_custom_surface_route_is_served() -> None:
    registry = RouteRegistry()
    for surface in default_surfaces(lambda: TOKEN):
        registry.add_surface(surface)
    registry.add_surface(_CustomSurface())
    async with running_server(registry=registry) as (server, _service):
        status, _headers, payload = await _request(server, "GET", f"/custom?token={TOKEN}")
        assert status == 200
        assert json.loads(payload) == {"ok": True, "surface": "custom"}


async def test_sse_stream_carries_frames() -> None:
    async with running_server() as (server, service):
        service.events = [
            TextEvent(session_id="s-1", text="hello"),
            FinalEvent(session_id="s-1", text="hello"),
        ]
        status, headers, _payload = await _request(
            server,
            "GET",
            f"/api/sessions/s-1/events?token={TOKEN}",
        )
        assert status == 200
        assert headers["content-type"].startswith("text/event-stream")
        events = await _collect_sse(server, "/api/sessions/s-1/events", token=TOKEN)
        assert [event["type"] for event in events] == ["text", "final"]
        assert events[0]["text"] == "hello"


async def test_api_routes_dispatched_to_service() -> None:
    async with running_server() as (server, service):
        status, _headers, payload = await _request(server, "POST", f"/api/sessions?token={TOKEN}", body=b"{}")
        assert status == 201
        assert json.loads(payload)["session_id"] == "s-1"
        status, _headers, _payload = await _request(
            server,
            "POST",
            f"/api/sessions/s-1/messages?token={TOKEN}",
            body=json.dumps({"text": "hello"}).encode(),
        )
        assert status == 202
        assert ("send", "s-1", "hello") in service.calls
        status, _headers, payload = await _request(
            server, "GET", f"/api/sessions/s-1/transcript?token={TOKEN}"
        )
        assert status == 200
        status, _headers, _payload = await _request(
            server, "POST", f"/api/sessions/s-1/resume?token={TOKEN}", body=b"{}"
        )
        assert status == 200
        status, _headers, payload = await _request(
            server, "POST", f"/api/sessions/s-1/interrupt?token={TOKEN}", body=b"{}"
        )
        assert status == 200
        assert json.loads(payload)["interrupted"] is False


async def test_graceful_shutdown_releases_port() -> None:
    server = WebServer(
        token=TOKEN,
        registry=RouteRegistry(),
        service_provider=lambda: FakeWebService(),
        port=0,
    )
    port = await server.start()
    serving = asyncio.create_task(server.serve_forever())
    await server.close()
    await asyncio.wait_for(serving, 5.0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOCALHOST, port))


# ---------------------------------------------------------------------------
# plugin seam
# ---------------------------------------------------------------------------


class _FakeRaw:
    def __init__(self) -> None:
        self.provided: dict[str, Any] = {}

    def provide(self, name: str, value: Any) -> Any:
        self.provided[name] = value
        return lambda: None

    def resolve_intercept(self, _name: str) -> dict[str, Any]:
        return {}


def _fake_ctx() -> Any:
    raw = _FakeRaw()
    return SimpleNamespace(raw=raw)


def test_plugin_is_inert_until_started() -> None:
    ctx = _fake_ctx()
    plugin = WebHostPlugin(ctx, None)
    assert plugin.host is None
    assert plugin.url is None
    assert plugin.token and len(plugin.token) >= 16
    assert ctx.raw.provided["web"] is plugin
    plugin.setup()
    assert plugin.host is None
    patterns = {route.pattern for route in plugin.registry.routes()}
    assert "/" in patterns
    assert "/api/health" in patterns
    plugin.register_surface(_CustomSurface())
    route, _params = plugin.registry.match(Method.GET, "/custom")
    assert route.name == "custom"


def test_plugin_config_defaults_loopback_only() -> None:
    config = WebHostConfig()
    assert config.host == LOCALHOST
    assert config.port == 0
    assert config.autostart is False


def test_cli_refuses_non_loopback_host() -> None:
    from deeppyro.web.__main__ import main

    with pytest.raises(SystemExit):
        main(["--host", "0.0.0.0"])
    with pytest.raises(SystemExit):
        main(["--host", "192.168.0.5"])


# ---------------------------------------------------------------------------
# protocol and import guards
# ---------------------------------------------------------------------------


def test_wire_events_are_snake_case() -> None:
    events = [
        SessionEvent(session_id="s"),
        StatusEvent(session_id="s", phase=StatusPhase.DONE),
        StepEvent(session_id="s", turn=1, step=1),
        TextEvent(session_id="s", text="x"),
        ThinkingEvent(session_id="s", text="x"),
        ToolCallEvent(session_id="s", call_id="c", name="n", arguments="{}"),
        ToolResultEvent(session_id="s", call_id="c", is_error=False, text="t"),
        FinalEvent(session_id="s", text="x"),
        WebUsageEvent(session_id="s", input_tokens=1, output_tokens=2, cached_input_tokens=3),
        ErrorEvent(session_id="s", message="m"),
    ]
    valid = {member.value for member in WebEventType}
    for event in events:
        payload = to_wire(event)
        assert payload["type"] in valid
        for key in payload:
            assert key == key.lower(), f"non snake_case key: {key}"
    usage = to_wire(events[8])
    assert usage["input_tokens"] == 1
    assert usage["cached_input_tokens"] == 3
    tool_call = to_wire(events[5])
    assert tool_call["call_id"] == "c"


async def test_journal_replays_after_sequence() -> None:
    journal = EventJournal(capacity=8)
    journal.publish(TextEvent(session_id="s", text="one"))
    journal.publish(TextEvent(session_id="s", text="two"))
    journal.publish(FinalEvent(session_id="s", text="done"))
    entries = journal.entries_after(1)
    assert [entry.seq for entry in entries] == [2, 3]
    stream = journal.stream(after=2)
    entry = await asyncio.wait_for(anext(stream), 5.0)
    assert entry is not None and entry.seq == 3
    await stream.aclose()
    journal.close()


FORBIDDEN_IMPORTS = (
    "textual",
    "aiohttp",
    "flask",
    "uvicorn",
    "fastapi",
    "starlette",
    "tornado",
    "httpx",
    "requests",
)


def test_web_modules_import_no_tui_v4_or_web_framework() -> None:
    files = sorted(WEB_DIR.glob("*.py"))
    assert files, "web package is missing"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                assert root not in FORBIDDEN_IMPORTS, f"{path.name} imports {name}"
                assert not name.startswith("deeppyro.tui"), f"{path.name} imports {name}"
                assert not name.startswith("deeppyro.v4"), f"{path.name} imports {name}"


# ---------------------------------------------------------------------------
# native session integration (scripted provider, no network)
# ---------------------------------------------------------------------------


class ScriptedProvider:
    name = "local"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[Any] = []
        self.failure: BaseException | None = None
        self.gate: asyncio.Event | None = None

    async def stream(self, request: Any):
        self.calls.append(request)
        if self.gate is not None:
            await self.gate.wait()
        for item in list(self.script):
            yield item
        if self.failure is not None:
            raise self.failure

    async def aclose(self) -> None:
        return None


def _config(tmp_path: Path) -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
        session_dir=tmp_path / "sessions",
    )


@pytest.fixture
def web_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "work"
    workspace.mkdir()
    provider = ScriptedProvider(
        [
            TextDelta("native "),
            TextDelta("answer"),
            UsageEvent(Usage(input_tokens=12, output_tokens=4)),
            Finish(StopReason.STOP),
        ]
    )
    monkeypatch.setattr(LlmPlugin, "native_provider", lambda self, name: provider)
    return SimpleNamespace(
        tmp_path=tmp_path,
        workspace=workspace,
        config=_config(tmp_path),
        provider=provider,
    )


@pytest.fixture
async def kernel_app(web_env: SimpleNamespace):
    app = await boot(web_env.config)
    try:
        yield app
    finally:
        await app.stop()


async def _client(host: WebHost) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=host.base_url, timeout=20.0, trust_env=False)


async def test_native_create_send_stream_and_replay(kernel_app: Any, web_env: SimpleNamespace) -> None:
    host = await WebHost.start(kernel_app=kernel_app, port=0, cwd=str(web_env.workspace))
    try:
        async with await _client(host) as client:
            created = await client.post("/api/sessions", params={"token": host.token}, json={})
            assert created.status_code == 201
            session_id = created.json()["session_id"]
            assert (await client.get("/api/health")).status_code == 401
            health = (await client.get("/api/health", params={"token": host.token})).json()
            assert health["status"] == "ok"
            status = (await client.get(f"/api/sessions/{session_id}", params={"token": host.token})).json()
            assert status["active"] is True
            assert status["model"] == "m"
            assert status["context_window"] == 32768
            send = await client.post(
                f"/api/sessions/{session_id}/messages",
                params={"token": host.token},
                json={"text": "hello web"},
            )
            assert send.status_code == 202
            assert send.json()["queued"] == 0
            frames = await _collect_sse(
                host.server,
                f"/api/sessions/{session_id}/events",
                token=host.token,
                until=lambda events: any(
                    event.get("type") == "status" and event.get("phase") == "done" for event in events
                ),
            )
            types = [event["type"] for event in frames]
            phases = [event.get("phase") for event in frames if event["type"] == "status"]
            assert "thinking" in phases
            assert "text" in types
            final = next(event for event in frames if event["type"] == "final")
            assert final["text"] == "native answer"
            usage = next(event for event in frames if event["type"] == "usage")
            assert usage["input_tokens"] == 12
            assert usage["output_tokens"] == 4
            assert usage["cached_input_tokens"] == 0
            status = (await client.get(f"/api/sessions/{session_id}", params={"token": host.token})).json()
            assert status["busy"] is False
            assert status["usage"]["input_tokens"] == 12
            assert status["context_tokens"] > 0
            transcript = (
                await client.get(f"/api/sessions/{session_id}/transcript", params={"token": host.token})
            ).json()
            text = " ".join(entry["text"] for entry in transcript["entries"])
            assert "hello web" in text
            assert "native answer" in text
            assert (
                await client.post(
                    f"/api/sessions/{session_id}/interrupt",
                    params={"token": host.token},
                    json={},
                )
            ).json()["interrupted"] is False
    finally:
        await host.stop()


async def test_native_interrupt_and_bounded_queue(kernel_app: Any, web_env: SimpleNamespace) -> None:
    web_env.provider.gate = asyncio.Event()
    host = await WebHost.start(kernel_app=kernel_app, port=0, cwd=str(web_env.workspace))
    gate = web_env.provider.gate
    try:
        async with await _client(host) as client:
            session_id = (await client.post("/api/sessions", params={"token": host.token}, json={})).json()[
                "session_id"
            ]
            first = await client.post(
                f"/api/sessions/{session_id}/messages",
                params={"token": host.token},
                json={"text": "first"},
            )
            assert first.status_code == 202
            busy_status: dict[str, Any] = {}
            for _ in range(100):
                busy_status = (
                    await client.get(f"/api/sessions/{session_id}", params={"token": host.token})
                ).json()
                if busy_status["busy"]:
                    break
                await asyncio.sleep(0.02)
            assert busy_status["busy"] is True
            second = await client.post(
                f"/api/sessions/{session_id}/messages",
                params={"token": host.token},
                json={"text": "second"},
            )
            assert second.status_code == 202
            assert second.json()["queued"] == 1
            interrupted = (
                await client.post(
                    f"/api/sessions/{session_id}/interrupt",
                    params={"token": host.token},
                    json={},
                )
            ).json()
            assert interrupted["interrupted"] is True
            assert interrupted["dropped"] == 1
            frames = await _collect_sse(
                host.server,
                f"/api/sessions/{session_id}/events",
                token=host.token,
                until=lambda events: any(
                    event.get("type") == "status" and event.get("phase") == "interrupted" for event in events
                ),
            )
            phases = [event.get("phase") for event in frames if event.get("type") == "status"]
            assert "interrupted" in phases
            gate.set()
    finally:
        await host.stop()


async def test_native_turn_queue_limit_returns_429(kernel_app: Any, web_env: SimpleNamespace) -> None:
    web_env.provider.gate = asyncio.Event()
    host = await WebHost.start(kernel_app=kernel_app, port=0, cwd=str(web_env.workspace))
    gate = web_env.provider.gate
    try:
        async with await _client(host) as client:
            session_id = (await client.post("/api/sessions", params={"token": host.token}, json={})).json()[
                "session_id"
            ]
            first = await client.post(
                f"/api/sessions/{session_id}/messages",
                params={"token": host.token},
                json={"text": "first"},
            )
            assert first.status_code == 202
            busy_status: dict[str, Any] = {}
            for _ in range(100):
                busy_status = (
                    await client.get(f"/api/sessions/{session_id}", params={"token": host.token})
                ).json()
                if busy_status["busy"]:
                    break
                await asyncio.sleep(0.02)
            assert busy_status["busy"] is True
            for index in range(8):
                queued = await client.post(
                    f"/api/sessions/{session_id}/messages",
                    params={"token": host.token},
                    json={"text": f"queued {index}"},
                )
                assert queued.status_code == 202
                assert queued.json()["queued"] == index + 1
            overflow = await client.post(
                f"/api/sessions/{session_id}/messages",
                params={"token": host.token},
                json={"text": "overflow"},
            )
            assert overflow.status_code == 429
            assert overflow.json()["error"]["code"] == "too_many_requests"
            interrupted = await client.post(
                f"/api/sessions/{session_id}/interrupt",
                params={"token": host.token},
                json={},
            )
            assert interrupted.json()["dropped"] == 8
            gate.set()
    finally:
        await host.stop()


async def test_native_resume_and_inactive_status(kernel_app: Any, web_env: SimpleNamespace) -> None:
    host = await WebHost.start(kernel_app=kernel_app, port=0, cwd=str(web_env.workspace))
    try:
        async with await _client(host) as client:
            first_id = host.active_session_id or ""
            second = await client.post("/api/sessions", params={"token": host.token}, json={})
            second_id = second.json()["session_id"]
            assert second_id != first_id
            listed = (await client.get("/api/sessions", params={"token": host.token})).json()
            ids = [row["session_id"] for row in listed["sessions"]]
            assert first_id in ids
            assert second_id in ids
            assert listed["active_session_id"] == second_id
            inactive = (await client.get(f"/api/sessions/{first_id}", params={"token": host.token})).json()
            assert inactive["active"] is False
            assert inactive["usage"]["input_tokens"] == 0
            resumed = await client.post(
                f"/api/sessions/{first_id}/resume",
                params={"token": host.token},
                json={},
            )
            assert resumed.status_code == 200
            assert resumed.json()["resumed"] is True
            missing = await client.get("/api/sessions/session-does-not-exist", params={"token": host.token})
            assert missing.status_code == 404
    finally:
        await host.stop()


async def test_plugin_seam_starts_host_through_kernel(web_env: SimpleNamespace) -> None:
    app = await boot(
        web_env.config,
        extra_plugins=[PluginSpec(id="web", name="deeppyro.web.plugin:WebHostPlugin")],
    )
    try:
        plugin = app.get(WebHostPlugin)
        assert plugin is not None
        assert plugin.host is None
        host = await plugin.start(port=0, cwd=str(web_env.workspace))
        try:
            assert host.port > 0
            assert plugin.url is not None
            async with httpx.AsyncClient(base_url=host.base_url, timeout=20.0, trust_env=False) as client:
                response = await client.get("/api/health", params={"token": plugin.token})
                assert response.status_code == 200
        finally:
            await plugin.stop()
        assert plugin.host is None
    finally:
        await app.stop()
