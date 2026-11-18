"""Optional OTLP/HTTP (JSON) trace export.

The local trace store is the source of truth; this module is a side channel that
forwards the same trace records to an OpenTelemetry collector. It is off by
default: nothing leaves the process unless the [observability.otlp] enabled flag
is true and an endpoint is configured.

Design constraints (see docs/ops/telemetry.md):

* Exporting never blocks and never raises into the agent. submit() only does a
  non-blocking queue put; a bounded queue makes overload visible through the
  dropped counter instead of growing without limit.
* A failed HTTP request is counted and logged, never retried inline.
* Only aggregate trace fields are exported (ids, provider/model, token counts,
  latency, finish reason, retry count). Prompt/output text, file paths and the
  plain session id are never attached as attributes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from deeppyro.observability.trace import ModelCallRecord, RunRecord, ToolCallRecord, TraceRecord

__all__ = ["OtlpExporter", "OtlpSettings"]

logger = logging.getLogger(__name__)

_SCOPE_NAME = "deeppyro.observability"
_SCOPE_VERSION = "0.3.0"
_TRACES_SUFFIX = "/v1/traces"
_SPAN_KIND_INTERNAL = 1
_SPAN_KIND_CLIENT = 3
_STATUS_OK = 1
_STATUS_ERROR = 2
_ERROR_FINISH_REASONS = frozenset({"error", "aborted"})
_DEFAULT_QUEUE_SIZE = 256
_DEFAULT_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class OtlpSettings:
    """Configuration for the optional OTLP/HTTP(JSON) exporter."""

    enabled: bool = False
    endpoint: str = ""
    service_name: str = "deeppyro"
    queue_size: int = _DEFAULT_QUEUE_SIZE
    timeout_s: float = _DEFAULT_TIMEOUT_S
    headers: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> OtlpSettings:
        """Build settings from the [observability.otlp] config table.

        Accepts both the camelCase spelling used by upstream configs and the
        snake_case spelling used by Python-native configs.
        """
        if not isinstance(raw, Mapping):
            return cls()
        endpoint = raw.get("endpoint") or raw.get("url") or ""
        service_name = raw.get("serviceName") or raw.get("service_name") or "deeppyro"
        headers: dict[str, str] = {}
        raw_headers = raw.get("headers")
        if isinstance(raw_headers, Mapping):
            headers = {str(key): str(value) for key, value in raw_headers.items()}
        return cls(
            enabled=raw.get("enabled") is True,
            endpoint=str(endpoint),
            service_name=str(service_name),
            queue_size=_positive_int(raw.get("queueSize", raw.get("queue_size")), _DEFAULT_QUEUE_SIZE),
            timeout_s=_positive_float(raw.get("timeoutSeconds", raw.get("timeout_s")), _DEFAULT_TIMEOUT_S),
            headers=headers,
        )

    @property
    def url(self) -> str:
        """The resolved traces endpoint, adding /v1/traces when omitted."""
        endpoint = self.endpoint.strip()
        if not endpoint:
            return ""
        base = endpoint.rstrip("/")
        return base if base.endswith(_TRACES_SUFFIX) else base + _TRACES_SUFFIX


class OtlpExporter:
    """Bounded, asynchronous OTLP/HTTP(JSON) span exporter.

    Args:
        settings: Resolved exporter configuration.
        transport: Optional httpx transport (tests inject httpx.MockTransport).
    """

    def __init__(self, settings: OtlpSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0
        self._sent = 0
        self._failed = 0

    @property
    def enabled(self) -> bool:
        """Whether export is both switched on and pointed at an endpoint."""
        return self.settings.enabled and bool(self.settings.url)

    @property
    def url(self) -> str:
        """The resolved traces endpoint."""
        return self.settings.url

    @property
    def queue_size(self) -> int:
        """Maximum number of queued spans before overload is dropped."""
        return self.settings.queue_size

    @property
    def dropped(self) -> int:
        """Spans dropped because export was off, busy, or the queue was full."""
        return self._dropped

    def submit(self, record: TraceRecord) -> bool:
        """Queue one trace record for export.

        Returns True when the record was queued. This never blocks and never
        raises: disabled exporters, calls without a running loop, calls from
        another loop, full queues and span mapping errors all count as drops.
        """
        if not self.enabled:
            return False
        try:
            span = span_from_record(record)
        except Exception:
            logger.warning("OTLP span mapping failed", exc_info=True)
            self._dropped += 1
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._dropped += 1
            return False
        try:
            queue = self._ensure_started(loop)
            queue.put_nowait(_envelope(span, self.settings.service_name))
        except asyncio.QueueFull:
            self._dropped += 1
            return False
        except Exception:
            logger.warning("OTLP enqueue failed", exc_info=True)
            self._dropped += 1
            return False
        return True

    async def flush(self) -> None:
        """Wait until every queued span has been attempted."""
        queue = self._queue
        if queue is not None:
            await queue.join()

    async def aclose(self) -> None:
        """Flush, stop the background worker and close the HTTP client."""
        await self.flush()
        worker = self._worker
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        client = self._client
        self._queue = None
        self._worker = None
        self._client = None
        self._loop = None
        if client is not None:
            await client.aclose()

    def stats(self) -> dict[str, Any]:
        """Current exporter counters for tests and diagnostics."""
        queue = self._queue
        return {
            "enabled": self.enabled,
            "url": self.url,
            "queued": queue.qsize() if queue is not None else 0,
            "dropped": self._dropped,
            "sent": self._sent,
            "failed": self._failed,
        }

    # -- internals -----------------------------------------------------
    def _ensure_started(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue[dict[str, Any]]:
        queue = self._queue
        if queue is not None:
            if self._loop is not loop:
                raise RuntimeError("OTLP exporter is bound to another event loop")
            return queue
        queue = asyncio.Queue(maxsize=max(1, self.settings.queue_size))
        self._queue = queue
        self._loop = loop
        self._client = httpx.AsyncClient(transport=self._transport, timeout=self.settings.timeout_s)
        self._worker = loop.create_task(self._run())
        return queue

    async def _run(self) -> None:
        queue = self._queue
        client = self._client
        if queue is None or client is None:
            return
        while True:
            payload = await queue.get()
            try:
                await self._send(client, payload)
            finally:
                queue.task_done()

    async def _send(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
        try:
            response = await client.post(self.settings.url, json=payload, headers=dict(self.settings.headers))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._failed += 1
            logger.warning("OTLP export request failed", exc_info=True)
            return
        if response.status_code >= 400:
            self._failed += 1
            logger.warning("OTLP export rejected with status %s", response.status_code)
        else:
            self._sent += 1


def span_from_record(record: TraceRecord) -> dict[str, Any]:
    """Map one local trace record to an OTLP/JSON span object."""
    if isinstance(record, ModelCallRecord):
        return _span(
            trace_id=record.trace_id,
            span_id=record.span_id.removeprefix("span_"),
            name=f"{record.provider}.{record.model}",
            kind=_SPAN_KIND_CLIENT,
            ts=record.ts,
            latency_ms=record.latency_ms,
            attributes=_model_attributes(record),
            status=_status_for_finish(record.finish_reason, record.error_code),
        )
    if isinstance(record, ToolCallRecord):
        return _span(
            trace_id=record.trace_id,
            span_id=record.span_id.removeprefix("span_"),
            name=f"tool.{record.tool}",
            kind=_SPAN_KIND_CLIENT,
            ts=record.ts,
            latency_ms=record.latency_ms,
            attributes=_tool_attributes(record),
            status=_status_for_tool(record.is_error, record.error_code),
        )
    return _span(
        trace_id=record.trace_id,
        span_id=_derived_span_id(record.run_id),
        name="run",
        kind=_SPAN_KIND_INTERNAL,
        ts=record.started_at,
        latency_ms=0,
        attributes=_run_attributes(record),
        status={"code": _STATUS_OK},
    )


def _span(
    *,
    trace_id: str,
    span_id: str,
    name: str,
    kind: int,
    ts: int,
    latency_ms: int,
    attributes: list[dict[str, Any]],
    status: dict[str, Any],
) -> dict[str, Any]:
    end = max(int(ts), 0)
    start = max(end - max(int(latency_ms), 0), 0)
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start * 1_000_000),
        "endTimeUnixNano": str(end * 1_000_000),
        "attributes": attributes,
        "status": status,
    }


def _model_attributes(record: ModelCallRecord) -> list[dict[str, Any]]:
    attributes = [
        _attribute("gen_ai.provider.name", record.provider),
        _attribute("gen_ai.request.model", record.model),
        _attribute("gen_ai.usage.input_tokens", record.input_tokens),
        _attribute("gen_ai.usage.output_tokens", record.output_tokens),
        _attribute("deeppyro.run_id", record.run_id),
        _attribute("deeppyro.turn_id", record.turn_id),
        _attribute("deeppyro.step_id", record.step_id),
        _attribute("deeppyro.finish_reason", record.finish_reason),
        _attribute("deeppyro.retry_count", record.retry_count),
        _attribute("deeppyro.latency_ms", record.latency_ms),
    ]
    if record.cached_input_tokens is not None:
        attributes.append(_attribute("gen_ai.usage.cached_input_tokens", record.cached_input_tokens))
    if record.reasoning_tokens is not None:
        attributes.append(_attribute("gen_ai.usage.reasoning_tokens", record.reasoning_tokens))
    if record.cost_usd is not None:
        attributes.append(_attribute("deeppyro.cost_usd", record.cost_usd))
    if record.error_code is not None:
        attributes.append(_attribute("deeppyro.error_code", record.error_code))
    return attributes


def _tool_attributes(record: ToolCallRecord) -> list[dict[str, Any]]:
    attributes = [
        _attribute("deeppyro.call_id", record.call_id),
        _attribute("gen_ai.tool.name", record.tool),
        _attribute("deeppyro.args_hash", record.args_hash),
        _attribute("deeppyro.result_size", record.result_size),
        _attribute("deeppyro.is_error", record.is_error),
        _attribute("deeppyro.retry_count", record.retry_count),
        _attribute("deeppyro.latency_ms", record.latency_ms),
        _attribute("deeppyro.run_id", record.run_id),
        _attribute("deeppyro.turn_id", record.turn_id),
        _attribute("deeppyro.step_id", record.step_id),
    ]
    for key, value in (
        ("deeppyro.capability", record.capability),
        ("deeppyro.risk", record.risk),
        ("deeppyro.error_code", record.error_code),
        ("deeppyro.idempotency_key", record.idempotency_key),
    ):
        if value is not None:
            attributes.append(_attribute(key, value))
    return attributes


def _run_attributes(record: RunRecord) -> list[dict[str, Any]]:
    return [
        _attribute("deeppyro.run_id", record.run_id),
        _attribute("gen_ai.provider.name", record.provider),
        _attribute("gen_ai.request.model", record.model),
        _attribute("deeppyro.started_at", record.started_at),
    ]


def _status_for_finish(finish_reason: str, error_code: str | None) -> dict[str, Any]:
    if finish_reason in _ERROR_FINISH_REASONS:
        return {"code": _STATUS_ERROR, "message": error_code or finish_reason}
    return {"code": _STATUS_OK}


def _status_for_tool(is_error: bool, error_code: str | None) -> dict[str, Any]:
    if not is_error:
        return {"code": _STATUS_OK}
    return {"code": _STATUS_ERROR, "message": error_code or "tool_error"}


def _derived_span_id(run_id: str) -> str:
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        any_value: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        any_value = {"intValue": str(value)}
    elif isinstance(value, float):
        any_value = {"doubleValue": value}
    else:
        any_value = {"stringValue": str(value)}
    return {"key": key, "value": any_value}


def _envelope(span: dict[str, Any], service_name: str) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attribute("service.name", service_name)]},
                "scopeSpans": [
                    {
                        "scope": {"name": _SCOPE_NAME, "version": _SCOPE_VERSION},
                        "spans": [span],
                    }
                ],
            }
        ]
    }


def _positive_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


def _positive_float(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return fallback
    return float(value)
