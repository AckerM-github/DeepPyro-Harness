"""Token/cost accounting service with an append-only JSONL ledger.

The service records one :class:`UsageRecord` per finished agent turn — provider,
model, input/output/cached tokens, latency, retries, stop reason and session
identity — and answers read-only aggregates by model, session and day for the
CLI/TUI to render. Records land in an append-only JSONL file under the state
directory (`DEEPPYRO_USAGE_DIR`, default `<home>/usage/usage.jsonl`) so a later
process can replay them.

Two reporting paths feed the ledger:

* the native loop resolves :class:`deeppyro.observability.protocols.UsageSink`
  from its service mapping and calls :meth:`UsageService.record_turn`;
* the v4 agent has no turn-end hook that runs after `turn/end`, so the service
  subscribes to `llm/stream`/`agent/pre-step`/`agent/turn-stopping`/
  `session/disposed` and folds durable session records through
  :class:`deeppyro.observability.session_facts.SessionRecordScanner`.

Storage is synchronous and lock-protected on the in-memory aggregates only:
each append is one `O_APPEND` `os.write` of a complete line, so two concurrent
turns never serialize each other on an `asyncio.Lock` (which would also force a
needless async API) and never interleave partial lines.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

from deeppyro.api import PluginConfig, Service
from deeppyro.config import ModelProfile
from deeppyro.hooks import AGENT_PRE_STEP, AGENT_TURN_STOPPING, LLM_STREAM, SESSION_DISPOSED, HookService
from deeppyro.observability.protocols import TurnUsage
from deeppyro.observability.session_facts import SessionRecordScanner, TurnFact
from deeppyro.paths import deeppyro_home

logger = logging.getLogger(__name__)

USAGE_DIR_ENV = "DEEPPYRO_USAGE_DIR"
"""Environment override for the state directory holding `usage.jsonl`."""


class UsageFormatError(ValueError):
    """A persisted usage line does not match the record schema."""


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One accounted agent turn."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    stop_reason: str = ""
    session_id: str = ""
    turn: int = 0
    step: int | None = None
    ts: int = 0
    run_id: str | None = None
    trace_id: str | None = None
    source: str = "native"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSONL row for this record."""
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cost": self.cost,
            "latency_ms": self.latency_ms,
            "retries": self.retries,
            "stop_reason": self.stop_reason,
            "session_id": self.session_id,
            "turn": self.turn,
            "step": self.step,
            "ts": self.ts,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UsageRecord:
        """Validate and rebuild one row; raises :class:`UsageFormatError` on bad input."""
        if not isinstance(data, Mapping):
            raise UsageFormatError("usage record must be a mapping")
        provider = data.get("provider")
        model = data.get("model")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise UsageFormatError("usage record requires string provider and model")
        return cls(
            provider=provider,
            model=model,
            input_tokens=_non_negative(data.get("input_tokens"), "input_tokens"),
            output_tokens=_non_negative(data.get("output_tokens"), "output_tokens"),
            cached_tokens=_non_negative(data.get("cached_tokens"), "cached_tokens"),
            cost=_cost(data.get("cost")),
            latency_ms=_non_negative(data.get("latency_ms"), "latency_ms"),
            retries=_non_negative(data.get("retries"), "retries"),
            stop_reason=_optional_text(data.get("stop_reason")) or "",
            session_id=_optional_text(data.get("session_id")) or "",
            turn=_non_negative(data.get("turn"), "turn"),
            step=_optional_non_negative(data.get("step"), "step"),
            ts=_non_negative(data.get("ts"), "ts"),
            run_id=_optional_text(data.get("run_id")),
            trace_id=_optional_text(data.get("trace_id")),
            source=_optional_text(data.get("source")) or "native",
        )


@dataclass(slots=True)
class UsageTotals:
    """Aggregate of UsageRecords, for one dimension or the whole service."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    retries: int = 0

    def add(self, record: UsageRecord) -> None:
        """Fold one record into the total."""
        self.requests += 1
        self.input_tokens += record.input_tokens
        self.output_tokens += record.output_tokens
        self.cached_tokens += record.cached_tokens
        self.cost += record.cost
        self.latency_ms += record.latency_ms
        self.retries += record.retries

    def to_dict(self) -> dict[str, int | float]:
        """Return a plain mapping for status displays and tests."""
        return {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "cost": self.cost,
            "latency_ms": self.latency_ms,
            "retries": self.retries,
        }


class UsageLog:
    """Append-only JSONL file holding :class:`UsageRecord` rows."""

    def __init__(self, root: str | Path | None = None, *, filename: str = "usage.jsonl") -> None:
        if root is None:
            override = os.environ.get(USAGE_DIR_ENV)
            root_path = Path(override) if override else deeppyro_home() / "usage"
        else:
            root_path = Path(root)
        self._path = root_path / filename

    @property
    def path(self) -> Path:
        """Absolute path of the ledger file."""
        return self._path

    def append(self, record: UsageRecord) -> None:
        """Append one row as a single atomic write; raises OSError on failure."""
        line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        payload = line.encode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            os.write(handle, payload)
        finally:
            os.close(handle)

    def read(self) -> list[UsageRecord]:
        """Read every valid row in order; malformed and blank lines are skipped."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        records: list[UsageRecord] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line.decode("utf-8"))
                records.append(UsageRecord.from_dict(row))
            except (UnicodeDecodeError, json.JSONDecodeError, UsageFormatError):
                logger.warning("skipping malformed usage line in %s", self._path)
        return records


class UsageService(Service):
    """Token/cost ledger exposed as the `usage` service.

    Costs come from a supplied :class:`deeppyro.config.ModelProfile` or, for
    turn reports, from the profile found in the kernel config by provider/model.
    Everything recorded in memory is also appended to the JSONL ledger; the
    in-memory view is authoritative for the current process and
    :meth:`replay` can rebuild it from disk.
    """

    provide = "usage"
    plugin_id = "usage"
    version = "0.2.0"
    inject: ClassVar[tuple[Any, ...]] = ("hooks",)
    description = "Token/cost accounting ledger with JSONL persistence and turn reporting."

    def __init__(self, ctx: Any, config: PluginConfig = None) -> None:
        super().__init__(ctx, config)
        self._log = UsageLog()
        self._records: list[UsageRecord] = []
        self._totals = UsageTotals()
        self._by_model: dict[str, UsageTotals] = {}
        self._by_session: dict[str, UsageTotals] = {}
        self._by_day: dict[str, UsageTotals] = {}
        self._profiles: dict[tuple[str, str], ModelProfile] = {}
        self._lock = threading.Lock()
        self._scanner: SessionRecordScanner | None = None
        self._agents: dict[str, Any] = {}
        self._disposers: list[Any] = []

    # -- wiring --------------------------------------------------------
    def setup(self) -> None:
        """Subscribe the v4 record reporter when a hook service is available."""
        hooks = self.ctx.get(HookService)
        if hooks is None:
            return
        self._load_profiles()
        self._scanner = SessionRecordScanner()
        for hook_name, handler in (
            (LLM_STREAM, self._on_stream),
            (AGENT_PRE_STEP, self._on_pre_step),
            (AGENT_TURN_STOPPING, self._on_turn_stopping),
            (SESSION_DISPOSED, self._on_disposed),
        ):
            self._subscribe(hooks, hook_name, handler)

    def _subscribe(self, hooks: HookService, hook_name: str, handler: Any) -> None:
        """Attach one listener now and hand its teardown to the plugin fiber.

        The listener is registered eagerly: a plugin-fiber effect only runs its
        callback when the fiber disposes, so an effect-wrapped hooks.on would
        subscribe at unload time instead of setup time.  The effect below still
        owns the unsubscription so a hot-unmount cannot leak listeners.
        """
        dispose = hooks.on(hook_name, handler)
        self._disposers.append(dispose)
        self.ctx.effect(lambda owns=dispose: owns, f"usage:{hook_name}")

    def close(self) -> None:
        """Release the hook subscriptions."""
        while self._disposers:
            disposer = self._disposers.pop()
            with contextlib.suppress(Exception):
                disposer()

    def stop(self) -> None:
        """Kernel teardown entry point (same effect as close)."""
        self.close()

    # -- recording -----------------------------------------------------
    def record(
        self,
        profile: ModelProfile | None = None,
        *,
        provider: str = "",
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_tokens: int = 0,
        latency_ms: int = 0,
        retries: int = 0,
        stop_reason: str = "",
        session_id: str = "",
        turn: int = 0,
        step: int | None = None,
        ts: int = 0,
        run_id: str | None = None,
        trace_id: str | None = None,
        source: str = "native",
    ) -> UsageRecord:
        """Account one response and return the immutable record.

        Pass a ModelProfile to read provider, model and prices from it, or the
        bare provider/model strings when no profile is configured.
        """
        inputs = _non_negative(input_tokens, "input_tokens")
        outputs = _non_negative(output_tokens, "output_tokens")
        cached = _non_negative(cached_tokens, "cached_tokens")
        if profile is not None:
            provider = provider or profile.provider
            model = model or profile.id
            cost = profile.estimate_cost(input_tokens=inputs, output_tokens=outputs, cached_tokens=cached)
        else:
            cost = 0.0
        record = UsageRecord(
            provider=provider,
            model=model or "unknown",
            input_tokens=inputs,
            output_tokens=outputs,
            cached_tokens=cached,
            cost=cost,
            latency_ms=_non_negative(latency_ms, "latency_ms"),
            retries=_non_negative(retries, "retries"),
            stop_reason=stop_reason,
            session_id=session_id,
            turn=_non_negative(turn, "turn"),
            step=step,
            ts=ts,
            run_id=run_id,
            trace_id=trace_id,
            source=source,
        )
        self._store(record)
        return record

    def record_turn(self, usage: TurnUsage, /) -> UsageRecord:
        """Account one finished turn reported through the UsageSink contract."""
        cost = usage.cost
        if cost <= 0:
            cost = self._estimate_cost(
                usage.provider, usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens
            )
        return self.record(
            provider=usage.provider,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_tokens=usage.cached_tokens,
            latency_ms=usage.latency_ms,
            retries=usage.retries,
            stop_reason=usage.stop_reason,
            session_id=usage.session_id,
            turn=usage.turn,
            step=usage.step,
            ts=usage.ts,
            run_id=usage.run_id,
            trace_id=usage.trace_id,
            source=usage.source,
        )

    def _store(self, record: UsageRecord) -> None:
        with self._lock:
            self._absorb(record)
        with contextlib.suppress(OSError):
            self._log.append(record)

    def _absorb(self, record: UsageRecord) -> None:
        """Fold one record into the in-memory aggregates; the caller holds the lock."""
        self._records.append(record)
        self._totals.add(record)
        self._by_model.setdefault(record.model, UsageTotals()).add(record)
        if record.session_id:
            self._by_session.setdefault(record.session_id, UsageTotals()).add(record)
        self._by_day.setdefault(_day_key(record.ts), UsageTotals()).add(record)

    # -- queries -------------------------------------------------------
    def records(self) -> tuple[UsageRecord, ...]:
        """All records in accounting order."""
        with self._lock:
            return tuple(self._records)

    def totals(self) -> UsageTotals:
        """Aggregate over every record (an independent copy)."""
        with self._lock:
            return replace(self._totals)

    def by_model(self) -> dict[str, UsageTotals]:
        """Per-model aggregates as independent copies."""
        with self._lock:
            return {model: replace(totals) for model, totals in self._by_model.items()}

    def by_session(self) -> dict[str, UsageTotals]:
        """Per-session aggregates as independent copies."""
        with self._lock:
            return {session: replace(totals) for session, totals in self._by_session.items()}

    def by_day(self) -> dict[str, UsageTotals]:
        """Per-UTC-day aggregates as independent copies."""
        with self._lock:
            return {day: replace(totals) for day, totals in self._by_day.items()}

    def summary(self) -> dict[str, Any]:
        """Plain report with totals and every aggregate dimension."""
        with self._lock:
            return {
                "totals": self._totals.to_dict(),
                "models": {model: totals.to_dict() for model, totals in self._by_model.items()},
                "sessions": {session: totals.to_dict() for session, totals in self._by_session.items()},
                "days": {day: totals.to_dict() for day, totals in self._by_day.items()},
            }

    def replay(self) -> tuple[UsageRecord, ...]:
        """Rebuild the in-memory view from the JSONL ledger without duplicating rows."""
        records = self._log.read()
        with self._lock:
            self._records = []
            self._totals = UsageTotals()
            self._by_model = {}
            self._by_session = {}
            self._by_day = {}
            for record in records:
                self._absorb(record)
        return tuple(records)

    def reset(self) -> None:
        """Forget every in-memory record (the JSONL ledger is kept)."""
        with self._lock:
            self._records.clear()
            self._totals = UsageTotals()
            self._by_model.clear()
            self._by_session.clear()
            self._by_day.clear()

    @property
    def log_path(self) -> Path:
        """Path of the JSONL ledger this service appends to."""
        return self._log.path

    # -- v4 hook reporting --------------------------------------------
    def _on_stream(self, payload: Mapping[str, Any]) -> None:
        """Track attempt statistics for v4 provider calls (llm/stream frames)."""
        try:
            agent = payload.get("agent")
            sid = getattr(agent, "session_id", None)
            frame = payload.get("frame")
            if not isinstance(sid, str) or not isinstance(frame, Mapping):
                return
            self._agents[sid] = agent
            if self._scanner is not None:
                self._scanner.observe_frame(sid, frame)
        except Exception:
            logger.exception("usage: failed to observe an llm/stream frame")

    async def _on_pre_step(self, payload: Mapping[str, Any], next_step: Any) -> Any:
        """Flush turns completed before the next step starts."""
        try:
            self._scan_agent(payload.get("agent"))
        except Exception:
            logger.exception("usage: failed to scan records before a step")
        return await next_step()

    def _on_turn_stopping(self, payload: Mapping[str, Any]) -> None:
        """Emit the completing turn before turn/end is appended."""
        try:
            agent = payload.get("agent")
            turn = payload.get("turn")
            self._scan_agent(agent, force_turn=turn if isinstance(turn, int) else None)
        except Exception:
            logger.exception("usage: failed to flush a stopping turn")

    def _on_disposed(self, payload: Mapping[str, Any]) -> None:
        """Flush every buffered turn when a session is disposed."""
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str):
            return
        agent = self._agents.get(session_id)
        if agent is None:
            return
        try:
            self._scan_agent(agent, force_all=True)
            if self._scanner is not None:
                self._scanner.forget_session(session_id)
            del self._agents[session_id]
        except Exception:
            logger.exception("usage: failed to flush a disposed session")

    def _scan_agent(self, agent: Any, *, force_turn: int | None = None, force_all: bool = False) -> None:
        scanner = self._scanner
        session = getattr(agent, "session", None)
        if scanner is None or session is None:
            return
        sid = getattr(session, "session_id", None)
        if isinstance(sid, str):
            self._agents[sid] = agent
        for fact in scanner.scan(session, force_turn=force_turn, force_all=force_all):
            self._report_fact(fact)

    def _report_fact(self, fact: TurnFact) -> None:
        self.record_turn(
            TurnUsage(
                provider=fact.provider,
                model=fact.model,
                session_id=fact.session_id,
                turn=fact.turn,
                input_tokens=fact.input_tokens,
                output_tokens=fact.output_tokens,
                cached_tokens=fact.cached_tokens,
                latency_ms=fact.latency_ms,
                retries=fact.retries,
                stop_reason=fact.stop_reason,
                ts=fact.ts,
                source="v4",
            )
        )

    # -- pricing -------------------------------------------------------
    def _load_profiles(self) -> None:
        config = self.ctx.get(_kernel_config())
        if config is None:
            return
        value = getattr(config, "value", None)
        providers = getattr(value, "providers", None)
        if not isinstance(providers, Mapping):
            return
        for provider_name, provider_config in providers.items():
            models = getattr(provider_config, "models", None)
            for model_config in models or ():
                with contextlib.suppress(Exception):
                    profile = ModelProfile.from_model_config(model_config, provider=str(provider_name))
                    self._profiles.setdefault((profile.provider, profile.id), profile)

    def _estimate_cost(
        self, provider: str, model: str, input_tokens: int, output_tokens: int, cached_tokens: int
    ) -> float:
        profile = self._profiles.get((provider, model)) or self._profiles.get(("", model))
        if profile is None:
            return 0.0
        with contextlib.suppress(Exception):
            return profile.estimate_cost(
                input_tokens=int(input_tokens),
                output_tokens=int(output_tokens),
                cached_tokens=int(cached_tokens),
            )
        return 0.0


def _kernel_config() -> Any:
    """Import the kernel config service lazily so this module stays lightweight."""
    from deeppyro.kernel.config_service import KernelConfig

    return KernelConfig


def _day_key(ts: int) -> str:
    """Return the UTC calendar day for a millisecond timestamp."""
    if ts <= 0:
        return "unknown"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts / 1000.0, tz=UTC).strftime("%Y-%m-%d")


def _non_negative(value: Any, field: str) -> int:
    """Coerce a non-negative integer field."""
    if value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise UsageFormatError(f"{field} must be an integer") from exc
    if number < 0:
        raise UsageFormatError(f"{field} must not be negative")
    return number


def _optional_non_negative(value: Any, field: str) -> int | None:
    """Coerce an optional non-negative integer field."""
    if value is None:
        return None
    return _non_negative(value, field)


def _cost(value: Any) -> float:
    """Coerce a finite non-negative cost."""
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UsageFormatError("cost must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise UsageFormatError("cost must be a finite non-negative number")
    return number


def _optional_text(value: Any) -> str | None:
    """Coerce an optional string field."""
    return value if isinstance(value, str) else None


__all__ = ["USAGE_DIR_ENV", "UsageFormatError", "UsageLog", "UsageRecord", "UsageService", "UsageTotals"]
