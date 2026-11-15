"""Streamable HTTP transport with mocked responses."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from deeppyro.mcp.jsonrpc import notification_message, request_message
from deeppyro.mcp.transport import HttpTransport, McpTransportError
from deeppyro.mcp.types import McpServerConfig

URL = "https://mcp.test/mcp"


def config() -> McpServerConfig:
    return McpServerConfig(name="remote", transport="http", url=URL)


def result(message: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}}


async def test_json_response_and_session_id_round_trip() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.headers))
        message = json.loads(request.content)
        body = httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}})
        body.headers["mcp-session-id"] = "session-1"
        return body

    with respx.mock:
        respx.post(URL).mock(side_effect=handler)
        transport = HttpTransport(config())
        await transport.start()
        try:
            response = await transport.request(request_message(1, "tools/list"), timeout=5)
            assert response.unwrap() == {"tools": []}
            assert transport.session_id == "session-1"
            await transport.request(request_message(2, "tools/list"), timeout=5)
        finally:
            await transport.close()

    assert seen[0]["accept"] == "application/json, text/event-stream"
    assert "mcp-session-id" not in seen[0]
    assert seen[1]["mcp-session-id"] == "session-1"


async def test_sse_response_is_parsed() -> None:
    payload = 'event: message\ndata: {"jsonrpc": "2.0", "id": 5, "result": {"value": 42}}\n\n'
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=payload)
        )
        transport = HttpTransport(config())
        await transport.start()
        try:
            response = await transport.request(request_message(5, "tools/list"), timeout=5)
            assert response.unwrap() == {"value": 42}
        finally:
            await transport.close()


async def test_notification_accepts_202() -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(202))
        transport = HttpTransport(config())
        await transport.start()
        try:
            await transport.notify(notification_message("notifications/initialized"))
        finally:
            await transport.close()


async def test_error_status_is_a_transport_error() -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500, text="boom"))
        transport = HttpTransport(config())
        await transport.start()
        try:
            with pytest.raises(McpTransportError) as excinfo:
                await transport.request(request_message(1, "tools/list"), timeout=5)
            assert "HTTP 500" in str(excinfo.value)
        finally:
            await transport.close()


async def test_missing_response_for_the_request_id() -> None:
    with respx.mock:
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"jsonrpc": "2.0", "id": 99, "result": {}})
        )
        transport = HttpTransport(config())
        await transport.start()
        try:
            with pytest.raises(McpTransportError):
                await transport.request(request_message(1, "tools/list"), timeout=5)
        finally:
            await transport.close()


async def test_request_answered_with_202_is_an_error() -> None:
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(202))
        transport = HttpTransport(config())
        await transport.start()
        try:
            with pytest.raises(McpTransportError):
                await transport.request(request_message(1, "tools/list"), timeout=5)
        finally:
            await transport.close()


async def test_connection_failure_is_a_transport_error() -> None:
    with respx.mock:
        respx.post(URL).mock(side_effect=httpx.ConnectError("no route"))
        transport = HttpTransport(config())
        await transport.start()
        try:
            with pytest.raises(McpTransportError) as excinfo:
                await transport.request(request_message(1, "tools/list"), timeout=5)
            assert "could not reach" in str(excinfo.value)
        finally:
            await transport.close()
