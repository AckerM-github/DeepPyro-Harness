"""Service contracts for usage reporting and trace export.

The native loop and the v4 hook plugins must report telemetry without importing
concrete plugin classes. These protocols are the seam: the loop resolves them
from its service mapping through :class:`deeppyro.api.services.ServiceBag`, and
the kernel binds their service names here so any implementation (the builtin
usage ledger, the observability telemetry plugin, a host fake) resolves by type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from deeppyro.api.services import bind_service_type
from deeppyro.observability.trace import ModelCallRecord, RunRecord, ToolCallRecord

__all__ = ["TraceExportSink", "TurnUsage", "UsageSink"]


@dataclass(frozen=True, slots=True)
class TurnUsage:
    """One accounted agent turn: tokens, latency, retries and stop outcome.

    The native loop reports one record per run (its single turn); the v4 hook
    reporter reports one record per completed turn. Provider/model and
    session/turn identity travel with the record so a ledger can aggregate by
    any axis without re-reading the session.
    """

    provider: str
    model: str
    session_id: str
    turn: int
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    retries: int = 0
    stop_reason: str = ""
    step: int | None = None
    cost: float = 0.0
    ts: int = 0
    run_id: str | None = None
    trace_id: str | None = None
    source: str = "native"


class UsageSink(Protocol):
    """Receives one :class:`TurnUsage` per finished agent turn."""

    def record_turn(self, usage: TurnUsage, /) -> object:
        """Account one finished turn; returns the stored record (implementation defined)."""
        ...


class TraceExportSink(Protocol):
    """Receives trace records the native loop already persisted locally."""

    def export_trace_record(self, record: RunRecord | ModelCallRecord | ToolCallRecord, /) -> None:
        """Forward one trace record to the optional external exporter (must not block)."""
        ...


bind_service_type(UsageSink, "usage")
bind_service_type(TraceExportSink, "trace_export")
