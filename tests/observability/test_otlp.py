"""Tests for the optional OTLP/HTTP(JSON) trace exporter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

import httpx

from deeppyro.observability.otlp import OtlpExporter, OtlpSettings, span_from_record
from deeppyro.observability.trace import ModelCallRecord, RunRecord, ToolCallRecord

RUN_ID = "run_" + "a" * 16
TRACE_ID = "b" * 32
TURN_ID = "turn_" + "c" * 16
STEP_ID = "step_" + "d" * 16
SPAN_ID = "span_" + "e" * 16
CALL_ID = "call_" + "f" * 16
PROMPT_HASH = "sha256:" + "1" * 64
ARGS_HASH = "sha256:" + "2" * 64
SESSION_ID = "session-11111111-2222-3333-4444-555555555555"
TS = 1_767_000_000_000


def _model_call(**overrides: Any) -> ModelCallRecord:
    values: dict[str, Any] = {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "run_id": RUN_ID,
        "turn_id": TURN_ID,
        "step_id": STEP_ID,
        "provider": "openai",
        "model": "gpt-x",
        "prompt_hash": PROMPT_HASH,
        "input_tokens": 100,
        "output_tokens": 20,
        "latency_ms": 7,
        "finish_reason": "stop",
        "retry_count": 2,
        "ts": TS,
        "cached_input_tokens": 64,
        "reasoning_tokens": 3,
        "cost_usd": 0.25,
    }
    values.update(overrides)
    return ModelCallRecord(**values)


def _tool_call(**overrides: Any) -> ToolCallRecord:
    values: dict[str, Any] = {
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "run_id": RUN_ID,
        "turn_id": TURN_ID,
        "step_id": STEP_ID,
        "call_id": CALL_ID,
        "tool": "read_file",
        "args_hash": ARGS_HASH,
        "result_size": 12,
        "is_error": False,
        "retry_count": 0,
        "latency_ms": 5,
        "ts": TS,
        "error_code": None,
        "idempotency_key": "idem:" + "3" * 64,
    }
    values.update(overrides)
    return ToolCallRecord(**values)


def _run() -> RunRecord:
    return RunRecord(
        run_id=RUN_ID,
        trace_id=TRACE_ID,
        session_id=SESSION_ID,
        started_at=TS,
        provider="openai",
        model="gpt-x",
    )


def _attributes(span: dict[str, Any]) -> dict[str, Any]:
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def _handler(recorded: list[httpx.Request], status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(status, json={})

    return handle


def test_settings_default_to_disabled_and_read_the_config_table() -> None:
    assert OtlpSettings().enabled is False
    assert OtlpSettings().url == ""
    assert OtlpSettings.from_config(None) == OtlpSettings()

    configured = OtlpSettings.from_config(
        {
            "enabled": True,
            "endpoint": "http://collector:4318",
            "serviceName": "deeppyro-test",
            "queueSize": 3,
            "timeoutSeconds": 1.5,
            "headers": {"x-token": 1},
        }
    )
    assert configured.enabled is True
    assert configured.url == "http://collector:4318/v1/traces"
    assert configured.service_name == "deeppyro-test"
    assert configured.queue_size == 3
    assert configured.timeout_s == 1.5
    assert configured.headers == {"x-token": "1"}

    # Only a boolean True switches export on; every other value stays off.
    assert OtlpSettings.from_config({"enabled": "yes", "endpoint": "http://x"}).enabled is False
    assert OtlpSettings.from_config({"url": "http://y"}).url == "http://y/v1/traces"
    assert OtlpSettings.from_config({"endpoint": "http://x/v1/traces/"}).url == "http://x/v1/traces"


def test_span_from_record_maps_model_call_fields() -> None:
    record = _model_call()
    span = span_from_record(record)
    assert span["traceId"] == TRACE_ID
    assert span["spanId"] == "e" * 16
    assert span["name"] == "openai.gpt-x"
    assert span["kind"] == 3
    assert span["startTimeUnixNano"] == str((TS - 7) * 1_000_000)
    assert span["endTimeUnixNano"] == str(TS * 1_000_000)
    assert span["status"] == {"code": 1}
    attributes = _attributes(span)
    assert attributes["gen_ai.provider.name"] == "openai"
    assert attributes["gen_ai.request.model"] == "gpt-x"
    assert attributes["gen_ai.usage.input_tokens"] == "100"
    assert attributes["gen_ai.usage.output_tokens"] == "20"
    assert attributes["gen_ai.usage.cached_input_tokens"] == "64"
    assert attributes["gen_ai.usage.reasoning_tokens"] == "3"
    assert attributes["deeppyro.retry_count"] == "2"
    assert attributes["deeppyro.finish_reason"] == "stop"
    assert attributes["deeppyro.run_id"] == RUN_ID
    # Privacy: the plain session id never leaves the process.
    assert "session_id" not in json.dumps(span)
    assert SESSION_ID not in json.dumps(span)


def test_span_from_record_marks_failures_and_maps_run_and_tool_spans() -> None:
    failed = span_from_record(_model_call(finish_reason="error", error_code="provider_error"))
    assert failed["status"] == {"code": 2, "message": "provider_error"}

    tool = span_from_record(_tool_call(is_error=True, error_code="ENOENT"))
    assert tool["name"] == "tool.read_file"
    assert tool["kind"] == 3
    assert tool["status"] == {"code": 2, "message": "ENOENT"}
    assert _attributes(tool)["deeppyro.call_id"] == CALL_ID
    assert _attributes(tool)["deeppyro.is_error"] is True

    run = span_from_record(_run())
    assert run["name"] == "run"
    assert run["kind"] == 1
    assert run["spanId"] == hashlib.sha256(RUN_ID.encode("utf-8")).hexdigest()[:16]
    assert run["status"] == {"code": 1}
    assert _attributes(run)["deeppyro.run_id"] == RUN_ID


def test_exporter_is_disabled_without_enabled_flag_or_endpoint() -> None:
    assert OtlpExporter(OtlpSettings()).enabled is False
    assert OtlpExporter(OtlpSettings(enabled=True)).enabled is False
    assert OtlpExporter(OtlpSettings(enabled=False, endpoint="http://collector:4318")).enabled is False


async def test_disabled_exporter_never_sends() -> None:
    recorded: list[httpx.Request] = []
    exporter = OtlpExporter(OtlpSettings(), transport=httpx.MockTransport(_handler(recorded)))
    assert exporter.submit(_model_call()) is False
    await exporter.flush()
    assert recorded == []
    assert exporter.stats()["sent"] == 0
    await exporter.aclose()


def test_submit_without_a_running_loop_is_counted_as_dropped() -> None:
    exporter = OtlpExporter(OtlpSettings(enabled=True, endpoint="http://collector:4318"))
    assert exporter.enabled is True
    assert exporter.submit(_model_call()) is False
    assert exporter.dropped == 1


async def test_enabled_exporter_posts_otlp_json_envelopes() -> None:
    recorded: list[httpx.Request] = []
    settings = OtlpSettings(enabled=True, endpoint="http://collector:4318", service_name="deeppyro-test")
    exporter = OtlpExporter(settings, transport=httpx.MockTransport(_handler(recorded)))
    try:
        assert exporter.submit(_model_call()) is True
        assert exporter.submit(_tool_call()) is True
        assert exporter.submit(_run()) is True
        await exporter.flush()

        assert len(recorded) == 3
        request = recorded[0]
        assert request.method == "POST"
        assert str(request.url) == "http://collector:4318/v1/traces"
        body = json.loads(request.content)
        resource = body["resourceSpans"][0]["resource"]
        assert resource["attributes"] == [{"key": "service.name", "value": {"stringValue": "deeppyro-test"}}]
        scope = body["resourceSpans"][0]["scopeSpans"][0]["scope"]
        assert scope["name"] == "deeppyro.observability"
        spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
        assert [span["name"] for span in spans] == ["openai.gpt-x"]
        assert spans[0]["traceId"] == TRACE_ID
        assert exporter.stats()["sent"] == 3
        assert exporter.stats()["failed"] == 0
    finally:
        await exporter.aclose()
    await exporter.aclose()


async def test_full_queue_drops_spans_instead_of_blocking() -> None:
    recorded: list[httpx.Request] = []
    settings = OtlpSettings(enabled=True, endpoint="http://collector:4318", queue_size=1)
    exporter = OtlpExporter(settings, transport=httpx.MockTransport(_handler(recorded)))
    try:
        assert exporter.submit(_model_call()) is True
        assert exporter.submit(_model_call()) is False
        assert exporter.dropped == 1
        await exporter.flush()
        assert len(recorded) == 1
        assert exporter.stats()["sent"] == 1
    finally:
        await exporter.aclose()


async def test_transport_failure_is_counted_and_never_raised() -> None:
    def explode(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("collector is down")

    settings = OtlpSettings(enabled=True, endpoint="http://collector:4318")
    exporter = OtlpExporter(settings, transport=httpx.MockTransport(explode))
    try:
        assert exporter.submit(_model_call()) is True
        await exporter.flush()
        assert exporter.stats()["failed"] == 1
        assert exporter.stats()["sent"] == 0
    finally:
        await exporter.aclose()


async def test_error_status_is_counted_as_failed_export() -> None:
    recorded: list[httpx.Request] = []
    settings = OtlpSettings(enabled=True, endpoint="http://collector:4318")
    exporter = OtlpExporter(settings, transport=httpx.MockTransport(_handler(recorded, status=503)))
    try:
        assert exporter.submit(_model_call()) is True
        await exporter.flush()
        assert exporter.stats()["failed"] == 1
        assert exporter.stats()["sent"] == 0
    finally:
        await exporter.aclose()
