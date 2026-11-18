"""Tests for the usage ledger: persistence, aggregates, turn reports and concurrency."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from deeppyro.builtin.usage import (
    USAGE_DIR_ENV,
    UsageFormatError,
    UsageRecord,
    UsageService,
)
from deeppyro.config import Config, ModelConfig, ModelCost, ModelProfile, ProviderConfig
from deeppyro.kernel.app import create_app
from deeppyro.kernel.context import RuntimeContext
from deeppyro.kernel.manifest import PluginSpec
from deeppyro.observability.protocols import TurnUsage

SESSION_A = "session-11111111-2222-3333-4444-555555555555"
SESSION_B = "session-66666666-7777-8888-9999-000000000000"
RUN_ID = "run_" + "a" * 16
TRACE_ID = "b" * 32


@pytest.fixture(autouse=True)
def usage_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep every ledger write inside the test's temporary directory."""
    target = tmp_path / "usage"
    monkeypatch.setenv(USAGE_DIR_ENV, str(target))
    return target


def _config() -> Config:
    return Config(
        providers={
            "local": ProviderConfig(
                base_url="http://127.0.0.1:8000/v1",
                models=[ModelConfig(id="m")],
            )
        },
        agent={"provider": "local", "model": "m"},
    )


def _profile() -> ModelProfile:
    return ModelProfile(
        id="m",
        provider="local",
        context_window=32768,
        cost=ModelCost(input_per_mtok=1.0, output_per_mtok=2.0, cached_input_per_mtok=0.5),
    )


def _ts(day: int = 2, hour: int = 3) -> int:
    return int(datetime(2026, 1, day, hour, 4, 5, tzinfo=UTC).timestamp() * 1000)


def _turn(**overrides: Any) -> TurnUsage:
    values: dict[str, Any] = {
        "provider": "local",
        "model": "m",
        "session_id": SESSION_A,
        "turn": 1,
        "input_tokens": 100,
        "output_tokens": 20,
        "cached_tokens": 64,
        "latency_ms": 25,
        "retries": 1,
        "stop_reason": "stop",
        "ts": _ts(),
        "run_id": RUN_ID,
        "trace_id": TRACE_ID,
        "source": "native",
    }
    values.update(overrides)
    return TurnUsage(**values)


def test_usage_service_records_tokens_and_costs() -> None:
    service = UsageService(RuntimeContext.create_root())
    profile = _profile()
    record = service.record(profile, input_tokens=1_000_000, output_tokens=500_000, cached_tokens=2_000_000)
    assert (record.provider, record.model) == ("local", "m")
    assert record.cost == pytest.approx(3.0)
    totals = service.totals()
    assert totals.requests == 1
    assert (totals.input_tokens, totals.output_tokens, totals.cached_tokens) == (
        1_000_000,
        500_000,
        2_000_000,
    )
    assert service.records() == (record,)
    assert service.by_model()["m"].cost == pytest.approx(3.0)
    assert service.summary()["totals"]["requests"] == 1

    service.record(provider="local", model="m", input_tokens=10)
    assert service.totals().requests == 2
    assert service.totals().cost == pytest.approx(3.0)

    service.reset()
    assert service.totals().requests == 0
    assert service.records() == ()
    assert service.by_model() == {}


def test_usage_service_rejects_negative_tokens() -> None:
    service = UsageService(RuntimeContext.create_root())
    with pytest.raises(ValueError, match="input_tokens"):
        service.record(input_tokens=-1)
    with pytest.raises(ValueError, match="output_tokens"):
        service.record(output_tokens=-1)
    with pytest.raises(ValueError, match="cached_tokens"):
        service.record(cached_tokens=-1)


def test_usage_totals_copy_is_independent() -> None:
    service = UsageService(RuntimeContext.create_root())
    service.record(provider="local", model="m", input_tokens=5)
    copy = service.by_model()["m"]
    copy.input_tokens = 999
    assert service.by_model()["m"].input_tokens == 5


def test_usage_service_persists_rows_and_replays_without_duplicating(usage_dir: Path) -> None:
    service = UsageService(RuntimeContext.create_root())
    service.record_turn(_turn(step=2))
    path = service.log_path
    assert path == usage_dir / "usage.jsonl"

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["provider"] == "local"
    assert row["cached_tokens"] == 64
    assert row["latency_ms"] == 25
    assert row["retries"] == 1
    assert row["stop_reason"] == "stop"
    assert row["session_id"] == SESSION_A
    assert (row["turn"], row["step"]) == (1, 2)
    assert row["run_id"] == RUN_ID
    assert row["trace_id"] == TRACE_ID
    assert row["source"] == "native"

    replayed = UsageService(RuntimeContext.create_root())
    assert replayed.records() == ()
    assert replayed.replay() == service.records()
    assert replayed.totals().cached_tokens == 64
    # replay rebuilds memory only: the ledger must not be rewritten.
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_usage_queries_aggregate_by_model_session_and_utc_day() -> None:
    service = UsageService(RuntimeContext.create_root())
    service.record(provider="local", model="m", input_tokens=10, session_id=SESSION_A, ts=_ts(day=2))
    service.record(provider="local", model="m", input_tokens=5, session_id=SESSION_B, ts=_ts(day=2))
    service.record(provider="remote", model="m2", input_tokens=7, session_id=SESSION_A, ts=_ts(day=3))
    service.record(provider="local", model="m", input_tokens=1)

    assert set(service.by_model()) == {"m", "m2"}
    assert service.by_model()["m"].requests == 3
    assert service.by_model()["m"].input_tokens == 16
    assert service.by_model()["m2"].input_tokens == 7
    assert set(service.by_session()) == {SESSION_A, SESSION_B}
    assert service.by_session()[SESSION_A].requests == 2
    assert set(service.by_day()) == {"2026-01-02", "2026-01-03", "unknown"}
    assert service.by_day()["2026-01-02"].requests == 2
    summary = service.summary()
    assert summary["totals"]["requests"] == 4
    assert summary["models"]["m"]["input_tokens"] == 16
    assert summary["sessions"][SESSION_B]["requests"] == 1
    assert summary["days"]["2026-01-03"]["requests"] == 1


def test_usage_turn_report_records_cached_tokens_and_durable_fields() -> None:
    service = UsageService(RuntimeContext.create_root())
    record = service.record_turn(_turn())
    assert record.cached_tokens == 64
    assert record.input_tokens == 100
    assert record.output_tokens == 20
    assert record.latency_ms == 25
    assert record.retries == 1
    assert record.source == "native"
    assert service.totals().cached_tokens == 64
    assert service.log_path.read_text(encoding="utf-8").strip()
    assert UsageRecord.from_dict(json.loads(service.log_path.read_text(encoding="utf-8"))) == record

    service.record_turn(_turn(cached_tokens=0, retries=0, stop_reason="tool_calls", source="v4"))
    assert service.totals().requests == 2
    assert service.totals().cached_tokens == 64
    assert service.records()[-1].stop_reason == "tool_calls"
    assert service.records()[-1].source == "v4"


def test_usage_replay_skips_malformed_ledger_lines(usage_dir: Path) -> None:
    service = UsageService(RuntimeContext.create_root())
    path = usage_dir / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(UsageRecord(provider="local", model="m", input_tokens=3).to_dict())
    path.write_text(good + "\n" + "this is not json\n" + "\n" + good + "\n", encoding="utf-8")

    replayed = service.replay()
    assert len(replayed) == 2
    assert service.totals().input_tokens == 6
    assert service.records() == replayed
    with pytest.raises(UsageFormatError):
        UsageRecord.from_dict({"provider": "local", "model": "m", "input_tokens": -1})


def test_usage_ledger_failure_keeps_in_memory_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = UsageService(RuntimeContext.create_root())

    def _fail(_record: UsageRecord) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr(service._log, "append", _fail)
    record = service.record_turn(_turn())
    assert record.input_tokens == 100
    assert service.totals().requests == 1
    assert service.totals().cached_tokens == 64


async def test_concurrent_turns_append_complete_lines_without_serializing() -> None:
    """Two turns share the service without an asyncio.Lock in the path.

    record_turn()/record() are synchronous and never await: the lock only guards
    the in-memory aggregates, and the JSONL append is one atomic O_APPEND
    os.write() of a complete line. Picking asyncio.Lock would force an async API
    and make two concurrent turns serialize on disk I/O; this test drives both
    turns from one TaskGroup and checks that every line is intact.
    """
    service = UsageService(RuntimeContext.create_root())
    started: list[int] = []

    async def turn(index: int) -> None:
        started.append(index)
        for step in range(25):
            service.record_turn(_turn(turn=index, step=step, input_tokens=step + 1))
            await asyncio.sleep(0)

    async with asyncio.TaskGroup() as group:
        group.create_task(turn(1))
        group.create_task(turn(2))

    assert sorted(started) == [1, 2]
    assert service.totals().requests == 50
    assert {record.turn for record in service.records()} == {1, 2}
    lines = service.log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 50
    rows = [json.loads(line) for line in lines]
    assert all(row["provider"] == "local" for row in rows)
    assert sum(1 for row in rows if row["turn"] == 1) == 25


async def test_usage_service_mounts_through_the_v3_plugin_path() -> None:
    app = await create_app(
        _config(),
        plugins=(
            PluginSpec(id="hooks", name="deeppyro.builtin.hooks:HooksPlugin"),
            PluginSpec(id="usage", name="deeppyro.builtin.usage:UsageService"),
        ),
    )
    try:
        service = app.require(UsageService)
        assert isinstance(service, UsageService)
        service.record(None, provider="local", model="m", input_tokens=12)
        assert service.totals().input_tokens == 12
    finally:
        await app.stop()
