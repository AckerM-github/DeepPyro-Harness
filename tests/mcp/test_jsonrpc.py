"""JSON-RPC framing helpers."""

from __future__ import annotations

import pytest

from deeppyro.mcp.jsonrpc import (
    INVALID_REQUEST,
    JSONRPC_VERSION,
    JsonRpcError,
    McpProtocolError,
    McpResponse,
    RequestIds,
    decode_message,
    is_response,
    notification_message,
    request_message,
    response_from_message,
)


def test_request_message_carries_id_method_and_params() -> None:
    message = request_message(7, "tools/list", {"cursor": "abc"})
    assert message == {
        "jsonrpc": JSONRPC_VERSION,
        "id": 7,
        "method": "tools/list",
        "params": {"cursor": "abc"},
    }


def test_request_message_omits_empty_params() -> None:
    assert request_message(1, "tools/list") == {"jsonrpc": JSONRPC_VERSION, "id": 1, "method": "tools/list"}


def test_notification_message_has_no_id() -> None:
    assert notification_message("notifications/initialized") == {
        "jsonrpc": JSONRPC_VERSION,
        "method": "notifications/initialized",
    }


def test_request_ids_are_monotonic() -> None:
    ids = RequestIds(start=3)
    assert [ids.next(), ids.next(), ids.next()] == [3, 4, 5]


def test_decode_message_rejects_invalid_json() -> None:
    with pytest.raises(McpProtocolError):
        decode_message("{not json")


def test_decode_message_rejects_other_versions() -> None:
    with pytest.raises(McpProtocolError):
        decode_message('{"jsonrpc": "1.0", "id": 1, "result": {}}')


def test_decode_message_rejects_non_objects() -> None:
    with pytest.raises(McpProtocolError):
        decode_message("[1, 2, 3]")


def test_is_response_distinguishes_notifications() -> None:
    assert is_response({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert is_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no"}})
    assert not is_response({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_response_from_result_message() -> None:
    response = response_from_message({"jsonrpc": "2.0", "id": 4, "result": {"tools": []}})
    assert response.ok
    assert response.unwrap() == {"tools": []}


def test_response_from_error_message() -> None:
    response = response_from_message(
        {"jsonrpc": "2.0", "id": 4, "error": {"code": -32601, "message": "nope"}}
    )
    assert not response.ok
    with pytest.raises(JsonRpcError) as excinfo:
        response.unwrap()
    assert excinfo.value.code == -32601
    assert "nope" in str(excinfo.value)


def test_error_message_with_a_malformed_code_falls_back() -> None:
    error = JsonRpcError.from_message({"error": {"code": "bad", "message": "boom"}})
    assert error.code == INVALID_REQUEST or isinstance(error.code, int)
    assert error.message == "boom"


def test_malformed_error_object_becomes_a_protocol_error() -> None:
    error = JsonRpcError.from_message({"error": "nope"})
    assert "malformed" in error.message


def test_mcp_response_defaults() -> None:
    response = McpResponse()
    assert response.ok and response.unwrap() is None
