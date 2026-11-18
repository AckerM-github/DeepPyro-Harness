"""Tests for the v4 telemetry plugin: trace records from hooks and session facts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from deeppyro.config import Config
from deeppyro.core.protocols import ToolResult
from deeppyro.hooks import (
    AGENT_PRE_STEP,
    AGENT_REQUEST,
    AGENT_TURN_STOPPING,
    LLM_STREAM,
    SESSION_DISPOSED,
    TOOLS_POST_EXECUTE,
    TOOLS_PRE_EXECUTE,
    HookService,
)
from deeppyro.observability.otlp import OtlpExporter, OtlpSettings
from deeppyro.observability.telemetry import TelemetryPlugin
from deeppyro.observability.trace import HASH_RE, ModelCallRecord, prompt_hash
from tests.agent.fakes import FakeSessionLog
from tests.service_doubles import service_lookup, service_require

PROVIDER = "fake-provider"
MODEL = "fake-model"
OTLP_ENDPOINT = "http://collector:4318"


class FakeCtx:
    """Minimal PluginContext: service lookup, provide and effect disposal."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.provided: dict[str, Any] = {}
        self.effects: list[str] = []
        self.disposers: list[Any] = []

    def provide(self, name: str, value: Any, check: Any = None) -> Any:
        self.provided[name] = value

        def dispose() -> None:
            self.provided.pop(name, None)

        return dispose

    def get(self, service: str | type[Any]) -> Any | None:
        return service_lookup(self._services, service)

    def require(self, service: type[Any]) -> Any:
        return service_require(self._services, service)

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        # Production semantics: effect() only records a teardown callback; the
        # callback runs at dispose time, never during setup.
        self.effects.append(label)
        self.disposers.append(execute)
        return execute


def _config(observability: dict[str, Any]) -> Config:
    return Config.model_validate({"observability": observability})


def _agent(tmp_path: Path) -> tuple[SimpleNamespace, FakeSessionLog]:
    session = FakeSessionLog(cwd=tmp_path)
    return SimpleNamespace(
        session=session, session_id=session.session_id, provider_name=PROVIDER, model=MODEL
    ), session


def _stream(agent: SimpleNamespace, frame: dict[str, Any]) -> dict[str, Any]:
    return {"agent": agent, "frame": frame, "provider": PROVIDER, "model": MODEL}


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        provider=PROVIDER,
        model=MODEL,
        system=None,
        messages=(),
        tools=(),
        max_tokens=None,
        temperature=None,
    )


def _append_assistant(session: FakeSessionLog, *, turn: int = 1, step: int = 1) -> None:
    message = {
        "id": "msg_" + "a" * 16,
        "role": "assistant",
        "content": [{"kind": "text", "text": "done"}],
        "source": {"kind": "model", "provider": PROVIDER, "model": MODEL},
    }
    session.append(
        "assistant/message",
        {
            "turn": turn,
            "step": step,
            "message": message,
            "usage": {
                "inputTokens": 120,
                "outputTokens": 30,
                "cacheReadTokens": 64,
                "cacheWriteTokens": 0,
                "totalTokens": 150,
                "reasoningTokens": 5,
            },
        },
    )


def _append_turn_end(session: FakeSessionLog, *, turn: int = 1, kind: str = "completed") -> None:
    session.append("turn/end", {"turn": turn, "reason": {"kind": kind}})


def _rows(directory: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.jsonl")):
        rows.extend(
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    return rows


async def _make_plugin(
    tmp_path: Path,
    observability: Mapping[str, Any] | None = None,
) -> tuple[TelemetryPlugin, HookService, FakeCtx, SimpleNamespace, FakeSessionLog]:
    traces = tmp_path / "traces"
    table = dict(observability if observability is not None else {"enabled": True, "dir": str(traces)})
    hooks = HookService()
    ctx = FakeCtx({"hooks": hooks, "config": SimpleNamespace(value=_config(table))})
    plugin = TelemetryPlugin(ctx, None)
    await plugin.setup()
    agent, session = _agent(tmp_path)
    return plugin, hooks, ctx, agent, session


def _derived_turn_id(session_id: str, turn: int) -> str:
    return "turn_" + hashlib.sha256(f"{session_id}:{turn}".encode()).hexdigest()[:16]


async def test_disabled_telemetry_provides_trace_export_without_tracing(tmp_path: Path) -> None:
    plugin, _hooks, ctx, agent, session = await _make_plugin(tmp_path, {"enabled": False})
    try:
        assert plugin.stats()["enabled"] is False
        assert plugin.trace_store is None
        assert ctx.provided["trace_export"] is plugin
        plugin.export_trace_record(
            ModelCallRecord(
                trace_id="b" * 32,
                span_id="span_" + "e" * 16,
                run_id="run_" + "a" * 16,
                turn_id="turn_" + "c" * 16,
                step_id="step_" + "d" * 16,
                provider=PROVIDER,
                model=MODEL,
                prompt_hash=prompt_hash({"a": 1}),
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                finish_reason="stop",
                retry_count=0,
                ts=1,
            )
        )
        assert not (tmp_path / "traces").exists()
    finally:
        await plugin.stop()
    del agent, session


async def test_v4_model_call_trace_carries_usage_retries_and_prompt_hash(tmp_path: Path) -> None:
    plugin, hooks, _ctx, agent, session = await _make_plugin(tmp_path)
    try:
        assert plugin.trace_store is not None
        # The first frame starts the record watermark: only newer records count.
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "start", "turn": 1, "step": 1}))
        # One failed attempt then a successful retry: attempts=2 => retry_count=1.
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "end", "finish": "error"}))
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "start", "turn": 1, "step": 1}))
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "end", "finish": "tool-calls"}))
        await hooks.waterfall(
            AGENT_REQUEST,
            {"agent": agent, "request": _request(), "turn": 1, "step": 1, "signal": None},
            lambda: None,
        )
        _append_assistant(session)
        _append_turn_end(session)
        # A new turn start flushes the completed turn into the trace store.
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "start", "turn": 2, "step": 1}))

        store = plugin.trace_store
        assert store is not None
        rows = _rows(Path(store.root))
        runs = [row for row in rows if row["type"] == "run"]
        calls = [row for row in rows if row["type"] == "model_call"]
        assert len(runs) == 1
        assert len(calls) == 1
        run, call = runs[0], calls[0]
        assert run["session_id"] == session.session_id
        assert (run["provider"], run["model"]) == (PROVIDER, MODEL)
        assert call["run_id"] == run["run_id"]
        assert call["trace_id"] == run["trace_id"]
        assert call["turn_id"] == _derived_turn_id(session.session_id, 1)
        assert call["step_id"].startswith("step_")
        assert (call["provider"], call["model"]) == (PROVIDER, MODEL)
        assert (call["input_tokens"], call["output_tokens"]) == (120, 30)
        assert call["cached_input_tokens"] == 64
        assert call["reasoning_tokens"] == 5
        assert call["retry_count"] == 1
        assert call["finish_reason"] == "tool_calls"
        assert call["latency_ms"] >= 0
        assert call["ts"] > 0
        expected_prompt = prompt_hash(
            {
                "provider": PROVIDER,
                "model": MODEL,
                "system": None,
                "messages": [],
                "tools": [],
                "max_tokens": None,
                "temperature": None,
            }
        )
        assert call["prompt_hash"] == expected_prompt
        assert HASH_RE.match(call["prompt_hash"])
    finally:
        await plugin.stop()


async def test_v4_tool_call_trace_records_error_result_and_derived_call_id(tmp_path: Path) -> None:
    plugin, hooks, _ctx, agent, session = await _make_plugin(tmp_path)
    try:
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "start", "turn": 1, "step": 1}))
        call = {"callId": "call_1", "name": "read_file", "arguments": '{"path":"a.txt"}'}
        pre = {"call": call, "tool": "read_file", "turn": 1, "step": 1, "agent": agent}
        await hooks.waterfall(TOOLS_PRE_EXECUTE, pre, lambda: None)
        result = ToolResult(text="boom", is_error=True, error={"code": "ENOENT"})
        await hooks.waterfall(TOOLS_POST_EXECUTE, {**pre, "result": result}, lambda: None)

        store = plugin.trace_store
        assert store is not None
        rows = _rows(Path(store.root))
        tools = [row for row in rows if row["type"] == "tool_call"]
        assert len(tools) == 1
        record = tools[0]
        expected_call_id = (
            "call_" + hashlib.sha256(f"{session.session_id}:1:1:call_1".encode()).hexdigest()[:16]
        )
        assert record["call_id"] == expected_call_id
        assert record["tool"] == "read_file"
        assert record["result_size"] == len(b"boom")
        assert record["is_error"] is True
        assert record["retry_count"] == 0
        assert record["error_code"] == "ENOENT"
        assert record["turn_id"] == _derived_turn_id(session.session_id, 1)
        assert record["latency_ms"] >= 0
        assert HASH_RE.match(record["args_hash"])
        assert record["idempotency_key"].startswith("idem:")
    finally:
        await plugin.stop()


async def test_turn_stopping_hook_flushes_a_turn_before_turn_end(tmp_path: Path) -> None:
    plugin, hooks, _ctx, agent, session = await _make_plugin(tmp_path)
    try:
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "start", "turn": 1, "step": 1}))
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "end", "finish": "stop"}))
        _append_assistant(session)
        await hooks.serial(
            AGENT_TURN_STOPPING,
            {"agent": agent, "turn": 1, "signal": None, "finalText": "done"},
        )

        store = plugin.trace_store
        assert store is not None
        calls = [row for row in _rows(Path(store.root)) if row["type"] == "model_call"]
        assert len(calls) == 1
        assert calls[0]["finish_reason"] == "stop"
        assert calls[0]["retry_count"] == 0
    finally:
        await plugin.stop()


async def test_pre_step_hook_flushes_completed_turns(tmp_path: Path) -> None:
    plugin, hooks, _ctx, agent, session = await _make_plugin(tmp_path)
    try:
        # The first pre-step starts the watermark before any record exists.
        await hooks.waterfall(AGENT_PRE_STEP, {"agent": agent, "turn": 1, "step": 1}, lambda: None)
        _append_assistant(session, turn=1)
        _append_turn_end(session, turn=1)
        await hooks.waterfall(AGENT_PRE_STEP, {"agent": agent, "turn": 2, "step": 1}, lambda: None)

        store = plugin.trace_store
        assert store is not None
        calls = [row for row in _rows(Path(store.root)) if row["type"] == "model_call"]
        assert len(calls) == 1
        assert calls[0]["turn_id"] == _derived_turn_id(session.session_id, 1)
    finally:
        await plugin.stop()


async def test_session_disposal_flushes_buffered_turns_and_forgets_state(tmp_path: Path) -> None:
    plugin, hooks, _ctx, agent, session = await _make_plugin(tmp_path)
    try:
        await hooks.emit(LLM_STREAM, _stream(agent, {"type": "start", "turn": 1, "step": 1}))
        _append_assistant(session)
        # No turn/end yet: disposal force-flushes the buffered turn.
        await hooks.emit(SESSION_DISPOSED, {"sessionId": session.session_id})

        store = plugin.trace_store
        assert store is not None
        calls = [row for row in _rows(Path(store.root)) if row["type"] == "model_call"]
        assert len(calls) == 1
        assert plugin.stats()["runs"] == 0
        # A second disposal is harmless (state was forgotten).
        await hooks.emit(SESSION_DISPOSED, {"sessionId": session.session_id})
    finally:
        await plugin.stop()


async def test_export_trace_record_forwards_native_spans_to_otlp(tmp_path: Path) -> None:
    plugin, _hooks, _ctx, _agent, _session = await _make_plugin(tmp_path, {"enabled": False})
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        return httpx.Response(200, json={})

    exporter = OtlpExporter(
        OtlpSettings(enabled=True, endpoint=OTLP_ENDPOINT),
        transport=httpx.MockTransport(handler),
    )
    plugin._otlp = exporter
    record = ModelCallRecord(
        trace_id="b" * 32,
        span_id="span_" + "e" * 16,
        run_id="run_" + "a" * 16,
        turn_id="turn_" + "c" * 16,
        step_id="step_" + "d" * 16,
        provider=PROVIDER,
        model=MODEL,
        prompt_hash=prompt_hash({"a": 1}),
        input_tokens=11,
        output_tokens=2,
        latency_ms=3,
        finish_reason="stop",
        retry_count=0,
        ts=1_767_000_000_000,
    )
    try:
        plugin.export_trace_record(record)
        await exporter.flush()
        assert len(recorded) == 1
        span = json.loads(recorded[0].content)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == f"{PROVIDER}.{MODEL}"
        assert span["traceId"] == "b" * 32

        class _Broken:
            def submit(self, _record: Any) -> bool:
                raise RuntimeError("exporter is broken")

        plugin._otlp = _Broken()  # telemetry must swallow exporter failures
        plugin.export_trace_record(record)
    finally:
        plugin._otlp = exporter
        await plugin.stop()


async def test_telemetry_defaults_to_enabled_and_uses_the_traces_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    traces = tmp_path / "env-traces"
    monkeypatch.setenv("DEEPPYRO_TRACES_DIR", str(traces))
    hooks = HookService()
    # No config service available: tracing defaults to enabled at the default root.
    ctx = FakeCtx({"hooks": hooks})
    plugin = TelemetryPlugin(ctx, None)
    await plugin.setup()
    try:
        stats = plugin.stats()
        assert stats["enabled"] is True
        assert stats["trace_dir"] == str(traces)
        assert ctx.provided["trace_export"] is plugin
        assert "telemetry:llm/stream" in ctx.effects
        assert plugin.otlp_exporter.stats()["enabled"] is False
    finally:
        await plugin.stop()
