"""v4 trace coverage and optional OTLP export, mounted as a kernel plugin.

The native loop writes its own trace file (see deeppyro.observability.trace).
The v4 agent has no such writer: it emits hooks (llm/stream, tools/*) and
durable session records. This plugin turns those signals into the same trace
record types and, when configured, forwards them (plus the native loop's
records arriving through the trace_export service) to an OTLP/HTTP(JSON)
collector.

Design notes:

* One plugin provides one service (trace_export) and attaches hook listeners.
  Neither the agent loop nor the CLI grows a telemetry branch.
* v4 turns are derived from session records through SessionRecordScanner so
  usage and trace agree on token accounting; retry counts come from the
  llm/stream start frames of a (turn, step).
* The local trace file stays authoritative; OTLP is optional and off by
  default. Every handler is defensive: telemetry failures are logged and never
  propagate into the agent.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import time
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.agent.budget import idempotency_key
from deeppyro.api.context import PluginContext
from deeppyro.api.plugin import Plugin
from deeppyro.core.ids import new_run_id, new_span_id, new_trace_id, now_ms
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
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.observability.otlp import OtlpExporter, OtlpSettings
from deeppyro.observability.session_facts import ModelCallFact, SessionRecordScanner, TurnFact
from deeppyro.observability.trace import (
    FINISH_REASONS,
    ModelCallRecord,
    RunRecord,
    ToolCallRecord,
    TraceStore,
    args_hash,
    hash_payload,
    prompt_hash,
)

__all__ = ["TelemetryPlugin"]

logger = logging.getLogger(__name__)

_SCOPE_NAME = "deeppyro.trace"
_UNKNOWN = "unknown"
_MAX_CACHED_AGENTS = 64


@dataclass(slots=True)
class _RunState:
    """Identity of the trace run that represents one v4 turn."""

    run_id: str
    trace_id: str
    provider: str
    model: str
    created: bool = False


@dataclass(slots=True)
class _ToolStart:
    """Bookkeeping captured before a v4 tool executes."""

    started: float
    args_hash: str


class TelemetryPlugin(Plugin):
    """Trace writer for the v4 agent plus the optional OTLP export sink."""

    plugin_id = "telemetry"
    version = "0.3.0"
    description = "v4 trace coverage and optional OTLP/HTTP(JSON) export"
    inject: tuple[str, ...] = ("hooks", "config")

    def __init__(self, ctx: PluginContext, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(ctx, config)
        self._enabled = False
        self._store: TraceStore | None = None
        self._otlp = OtlpExporter(OtlpSettings())
        self._scanner = SessionRecordScanner()
        self._agents: dict[str, Any] = {}
        self._disposers: list[Callable[[], Any]] = []
        self._runs: dict[tuple[str, int], _RunState] = {}
        self._prompts: dict[tuple[str, int, int], str] = {}
        self._tools: dict[tuple[str, int, int, str], _ToolStart] = {}

    # -- plugin lifecycle ----------------------------------------------
    async def setup(self) -> None:
        """Read observability config, provide trace_export and attach hooks."""
        raw = self._config_table()
        self._enabled = raw.get("enabled") is not False
        root = _text_or_none(raw.get("dir") or raw.get("traceDir") or raw.get("trace_dir"))
        if self._enabled:
            self._store = TraceStore(Path(root)) if root is not None else TraceStore()
        otlp_raw = raw.get("otlp")
        self._otlp = OtlpExporter(
            OtlpSettings.from_config(otlp_raw if isinstance(otlp_raw, Mapping) else None)
        )
        self._own(self.ctx.provide("trace_export", self), "telemetry:trace-export")
        if not self._enabled:
            return
        hooks = self.ctx.require(HookService)
        for hook_name, handler in self._emit_handlers():
            self._own(hooks.on(hook_name, handler), f"telemetry:{hook_name}")
        for hook_name, waterfall in self._waterfall_handlers():
            self._own(hooks.on(hook_name, waterfall), f"telemetry:{hook_name}")
        self._own(hooks.on(AGENT_TURN_STOPPING, self._on_turn_stopping), f"telemetry:{AGENT_TURN_STOPPING}")

    def _own(self, dispose: Callable[[], Any], label: str) -> None:
        """Keep a registration disposal under the plugin fiber.

        Registrations are made eagerly by the caller.  Effect callbacks only run
        when the fiber disposes, which is what we want for the teardown here but
        not for the registration itself.
        """
        self._disposers.append(dispose)
        self.ctx.effect(lambda owns=dispose: owns, label)

    async def stop(self) -> None:
        """Detach the listeners, then flush and close the optional exporter."""
        while self._disposers:
            dispose = self._disposers.pop()
            with contextlib.suppress(Exception):
                dispose()
        await self._otlp.aclose()
        self._store = None

    # -- services ------------------------------------------------------
    def export_trace_record(self, record: RunRecord | ModelCallRecord | ToolCallRecord, /) -> None:
        """Forward a trace record the native loop already persisted locally."""
        try:
            self._otlp.submit(record)
        except Exception:
            logger.warning("OTLP submit failed", exc_info=True)

    @property
    def trace_store(self) -> TraceStore | None:
        """The local trace store, or None when tracing is disabled."""
        return self._store

    @property
    def otlp_exporter(self) -> OtlpExporter:
        """The optional external exporter."""
        return self._otlp

    def stats(self) -> dict[str, Any]:
        """Current telemetry state for diagnostics and tests."""
        store = self._store
        return {
            "enabled": self._enabled,
            "trace_dir": str(store.root) if store is not None else None,
            "runs": len(self._runs),
            "otlp": self._otlp.stats(),
        }

    # -- hook handlers: emitted hooks ----------------------------------
    def _emit_handlers(self) -> Generator[tuple[str, Callable[[Mapping[str, Any]], None]], None, None]:
        yield LLM_STREAM, self._on_stream
        yield SESSION_DISPOSED, self._on_disposed

    def _on_stream(self, payload: Mapping[str, Any]) -> None:
        try:
            agent = payload.get("agent")
            session_id = _session_id_of(agent)
            if session_id is not None:
                self._remember_agent(session_id, agent)
            frame = payload.get("frame")
            if session_id is None or not isinstance(frame, Mapping):
                return
            self._scanner.observe_frame(session_id, frame)
            if frame.get("type") == "start":
                self._flush_agent(agent)
        except Exception:
            logger.warning("llm/stream telemetry failed", exc_info=True)

    def _on_disposed(self, payload: Mapping[str, Any]) -> None:
        try:
            session_id = payload.get("sessionId")
            if not isinstance(session_id, str):
                return
            agent = self._agents.get(session_id)
            if agent is not None:
                self._flush_agent(agent, force_all=True)
            self._forget_session(session_id)
        except Exception:
            logger.warning("session/disposed telemetry failed", exc_info=True)

    # -- hook handlers: waterfall hooks --------------------------------
    def _waterfall_handlers(self) -> Generator[tuple[str, Callable[..., Any]], None, None]:
        yield AGENT_REQUEST, self._on_request
        yield AGENT_PRE_STEP, self._on_pre_step
        yield TOOLS_PRE_EXECUTE, self._on_tool_pre
        yield TOOLS_POST_EXECUTE, self._on_tool_post

    async def _on_request(self, payload: Mapping[str, Any], next_step: Callable[[], Any]) -> Any:
        try:
            agent = payload.get("agent")
            session_id = _session_id_of(agent)
            turn = _as_int(payload.get("turn"))
            step = _as_int(payload.get("step"))
            if session_id is not None and turn is not None and step is not None:
                self._prompts[(session_id, turn, step)] = _request_prompt_hash(payload.get("request"))
        except Exception:
            logger.warning("agent/request telemetry failed", exc_info=True)
        return await next_step()

    async def _on_pre_step(self, payload: Mapping[str, Any], next_step: Callable[[], Any]) -> Any:
        try:
            self._flush_agent(payload.get("agent"))
        except Exception:
            logger.warning("agent/pre-step telemetry failed", exc_info=True)
        return await next_step()

    async def _on_tool_pre(self, payload: Mapping[str, Any], next_step: Callable[[], Any]) -> Any:
        try:
            self._remember_tool_start(payload)
        except Exception:
            logger.warning("tools/pre-execute telemetry failed", exc_info=True)
        return await next_step()

    async def _on_tool_post(self, payload: Mapping[str, Any], next_step: Callable[[], Any]) -> Any:
        decision = await next_step()
        try:
            self._record_tool(payload)
        except Exception:
            logger.warning("tools/post-execute telemetry failed", exc_info=True)
        return decision

    # -- hook handlers: serial hooks -----------------------------------
    async def _on_turn_stopping(self, payload: Mapping[str, Any]) -> None:
        try:
            self._flush_agent(payload.get("agent"), force_turn=_as_int(payload.get("turn")))
        except Exception:
            logger.warning("agent/turn-stopping telemetry failed", exc_info=True)

    # -- v4 turn accounting --------------------------------------------
    def _remember_agent(self, session_id: str, agent: Any) -> None:
        if len(self._agents) >= _MAX_CACHED_AGENTS and session_id not in self._agents:
            self._agents.pop(next(iter(self._agents)), None)
        self._agents[session_id] = agent

    def _flush_agent(self, agent: Any, *, force_turn: int | None = None, force_all: bool = False) -> None:
        session = getattr(agent, "session", None)
        if session is None:
            return
        facts = self._scanner.scan(session, force_turn=force_turn, force_all=force_all)
        for fact in facts:
            self._report_turn(fact)

    def _report_turn(self, fact: TurnFact) -> None:
        provider = fact.provider or _UNKNOWN
        model = fact.model or _UNKNOWN
        state = self._ensure_run(fact.session_id, fact.turn, provider, model)
        try:
            for call in fact.calls:
                self._append(self._model_record(state, fact, call))
        finally:
            self._drop_turn_state(fact.session_id, fact.turn)

    def _model_record(self, state: _RunState, fact: TurnFact, call: ModelCallFact) -> ModelCallRecord:
        finish_reason = call.finish_reason if call.finish_reason in FINISH_REASONS else "stop"
        prompt = self._prompts.get((fact.session_id, fact.turn, call.step))
        return ModelCallRecord(
            trace_id=state.trace_id,
            span_id=new_span_id(),
            run_id=state.run_id,
            turn_id=_turn_id(fact.session_id, fact.turn),
            step_id=_step_id(fact.session_id, fact.turn, call.step),
            provider=call.provider or state.provider,
            model=call.model or state.model,
            prompt_hash=prompt if prompt is not None else hash_payload({}),
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            latency_ms=call.latency_ms,
            finish_reason=finish_reason,
            retry_count=call.retries,
            ts=call.ts or now_ms(),
            cached_input_tokens=call.cached_tokens,
            reasoning_tokens=call.reasoning_tokens,
            error_code="provider_error" if finish_reason == "error" else None,
        )

    def _ensure_run(self, session_id: str, turn: int, provider: str, model: str) -> _RunState:
        key = (session_id, turn)
        state = self._runs.get(key)
        if state is not None:
            return state
        state = _RunState(
            run_id=new_run_id(),
            trace_id=new_trace_id(),
            provider=provider or _UNKNOWN,
            model=model or _UNKNOWN,
        )
        self._runs[key] = state
        store = self._store
        if store is not None:
            state.created = True
            try:
                store.create_run(
                    RunRecord(
                        run_id=state.run_id,
                        trace_id=state.trace_id,
                        session_id=session_id,
                        started_at=now_ms(),
                        provider=state.provider,
                        model=state.model,
                    )
                )
            except Exception:
                logger.warning("trace run record was not created", exc_info=True)
        return state

    def _append(self, record: ModelCallRecord | ToolCallRecord) -> None:
        store = self._store
        if store is not None:
            try:
                store.append(record)
            except Exception:
                logger.warning("trace record was not appended", exc_info=True)
        self._otlp.submit(record)

    def _drop_turn_state(self, session_id: str, turn: int) -> None:
        self._prompts = {key: value for key, value in self._prompts.items() if key[:2] != (session_id, turn)}
        self._tools = {key: value for key, value in self._tools.items() if key[:2] != (session_id, turn)}

    def _forget_session(self, session_id: str) -> None:
        self._scanner.forget_session(session_id)
        self._agents.pop(session_id, None)
        self._runs = {key: value for key, value in self._runs.items() if key[0] != session_id}
        self._prompts = {key: value for key, value in self._prompts.items() if key[0] != session_id}
        self._tools = {key: value for key, value in self._tools.items() if key[0] != session_id}

    # -- v4 tool accounting --------------------------------------------
    def _remember_tool_start(self, payload: Mapping[str, Any]) -> None:
        session_id = _session_id_of(payload.get("agent"))
        call = payload.get("call")
        if session_id is None or not isinstance(call, Mapping):
            return
        call_id = call.get("callId")
        if not isinstance(call_id, str):
            return
        turn = _as_int(payload.get("turn")) or 0
        step = _as_int(payload.get("step")) or 0
        key = (session_id, turn, step, call_id)
        self._tools[key] = _ToolStart(
            started=time.monotonic(), args_hash=_arguments_hash(call.get("arguments"))
        )

    def _record_tool(self, payload: Mapping[str, Any]) -> None:
        session_id = _session_id_of(payload.get("agent"))
        call = payload.get("call")
        if session_id is None or not isinstance(call, Mapping):
            return
        call_id = call.get("callId")
        tool = call.get("name")
        if not isinstance(call_id, str) or not isinstance(tool, str) or not tool:
            return
        turn = _as_int(payload.get("turn")) or 0
        step = _as_int(payload.get("step")) or 0
        provider, model = _agent_identity(payload.get("agent"))
        state = self._ensure_run(session_id, turn, provider, model)
        start = self._tools.pop((session_id, turn, step, call_id), None)
        latency_ms = 0
        if start is not None:
            latency_ms = max(0, int((time.monotonic() - start.started) * 1000.0))
        digest = start.args_hash if start is not None else _arguments_hash(call.get("arguments"))
        result = payload.get("result")
        record = ToolCallRecord(
            trace_id=state.trace_id,
            span_id=new_span_id(),
            run_id=state.run_id,
            turn_id=_turn_id(session_id, turn),
            step_id=_step_id(session_id, turn, step),
            call_id=_tool_call_id(session_id, turn, step, call_id),
            tool=tool,
            args_hash=digest,
            result_size=_result_size(result),
            is_error=bool(getattr(result, "is_error", False)),
            retry_count=0,
            latency_ms=latency_ms,
            ts=now_ms(),
            error_code=_error_code(result),
            idempotency_key=idempotency_key(session_id, call_id, digest),
        )
        self._append(record)

    # -- config --------------------------------------------------------
    def _config_table(self) -> Mapping[str, Any]:
        raw = self.config
        if isinstance(raw, Mapping) and raw:
            return raw
        config_service: KernelConfig | None = None
        with contextlib.suppress(Exception):
            config_service = self.ctx.get(KernelConfig)
        if config_service is None:
            return {}
        value = getattr(config_service, "value", None)
        table = getattr(value, "observability", None)
        return table if isinstance(table, Mapping) else {}


def _request_prompt_hash(request: Any) -> str:
    with contextlib.suppress(Exception):
        return prompt_hash(_request_payload(request))
    return hash_payload({"provider": _UNKNOWN})


def _request_payload(request: Any) -> dict[str, Any]:
    messages = getattr(request, "messages", ())
    tools = getattr(request, "tools", ())
    return {
        "provider": getattr(request, "provider", ""),
        "model": getattr(request, "model", ""),
        "system": getattr(request, "system", None),
        "messages": [_dump(item) for item in messages],
        "tools": [_dump(item) for item in tools],
        "max_tokens": getattr(request, "max_tokens", None),
        "temperature": getattr(request, "temperature", None),
    }


def _dump(item: Any) -> Any:
    dumper = getattr(item, "model_dump", None)
    if callable(dumper):
        with contextlib.suppress(Exception):
            return dumper(mode="json")
        with contextlib.suppress(Exception):
            return dumper()
        return {}
    if isinstance(item, Mapping):
        return {str(key): _plain(value) for key, value in item.items()}
    return str(item)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _arguments_hash(arguments: Any) -> str:
    digest: str | None = None
    if isinstance(arguments, str):
        with contextlib.suppress(Exception):
            digest = args_hash(arguments)
    if digest is not None:
        return digest
    with contextlib.suppress(Exception):
        return hash_payload({"arguments": _plain(arguments)})
    return hash_payload({"arguments": str(arguments)[:256]})


def _result_size(result: Any) -> int:
    text = getattr(result, "text", "")
    if isinstance(text, str):
        return len(text.encode("utf-8"))
    return 0


def _error_code(result: Any) -> str | None:
    error = getattr(result, "error", None)
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    return None


def _session_id_of(agent: Any) -> str | None:
    session_id = getattr(agent, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def _agent_identity(agent: Any) -> tuple[str, str]:
    provider = getattr(agent, "provider_name", None)
    model = getattr(agent, "model", None)
    return (
        provider if isinstance(provider, str) and provider else _UNKNOWN,
        model if isinstance(model, str) and model else _UNKNOWN,
    )


def _turn_id(session_id: str, turn: int) -> str:
    return "turn_" + _digest16(session_id, str(turn))


def _step_id(session_id: str, turn: int, step: int) -> str:
    return "step_" + _digest16(session_id, str(turn), str(step))


def _tool_call_id(session_id: str, turn: int, step: int, call_id: str) -> str:
    return "call_" + _digest16(session_id, str(turn), str(step), call_id)


def _digest16(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
