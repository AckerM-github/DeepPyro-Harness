"""Test doubles for the agent loop.

The real session writer (deeppyro.session) lands in another workstream, so
these tests drive AgentSession and Compactor through a fake log that mirrors
the durable contract: append-only records with seqs and envelopes, and a
derive_messages() that applies the surface fold (replacements shadow the
earlier range and surface the checkpoint in its place).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.core.ids import new_session_id, now_ms
from deeppyro.core.protocols import (
    Finish,
    LlmRequest,
    StreamEvent,
    ToolContext,
    ToolResult,
    ToolSchema,
)
from deeppyro.core.wire import LogRecord, Message, SessionHeader
from deeppyro.session.derive import derive_messages as derive_session_messages
from deeppyro.session.events import SURFACE_EVENTS

# Historical alias: the fake log now stores real LogRecord envelopes.
FakeRecord = LogRecord


class FakeSessionLog:
    """A minimal SessionLogLike that honors surface envelopes."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        cwd: Path | str = ".",
        created_at: int | None = None,
    ) -> None:
        self.session_id = session_id or new_session_id()
        self.header = SessionHeader(
            id=self.session_id,
            createdAt=created_at if created_at is not None else now_ms(),
            cwd=str(cwd),
        )
        self.records: list[LogRecord] = []
        self.closed = False
        self.appends = 0

    # -- SessionLogLike ----------------------------------------------------
    def append(self, type: str, data: dict[str, Any], **envelope: Any) -> int:
        seq = len(self.records) + 1
        surface_op = envelope.get("surfaceOp")
        if surface_op is None and type in SURFACE_EVENTS:
            surface_op = "append"
        self.records.append(
            LogRecord(
                type=type,
                seq=seq,
                data=data,
                time=now_ms(),
                surfaceOp=surface_op,
                sourceEventSeqs=envelope.get("sourceEventSeqs"),
                ignorable=envelope.get("ignorable"),
            )
        )
        self.appends += 1
        return seq

    def derive_messages(self) -> list[Message]:
        # Mirror the production Session: the canonical surface fold validates
        # envelopes and drops empty system/developer/assistant messages.
        return derive_session_messages(self.records)

    def close(self) -> None:
        self.closed = True

    # -- assertions helpers ------------------------------------------------
    def types(self) -> list[str]:
        return [record.type for record in self.records]

    def records_of(self, record_type: str) -> list[LogRecord]:
        return [record for record in self.records if record.type == record_type]

    def seq_of(self, record: LogRecord) -> int:
        return record.seq

    def seq_for(self, record_type: str, *, index: int = 0) -> int:
        return self.records_of(record_type)[index].seq


@dataclass
class ScriptedTurn:
    """One provider stream: events, an optional pause, or a raised error."""

    events: list[StreamEvent] = field(default_factory=list)
    pause: asyncio.Event | None = None
    pause_after: int = 0
    error: BaseException | None = None


class ScriptedProvider:
    """A scripted LlmProvider that records the requests it receives."""

    name = "fake-provider"

    def __init__(self, script: list[ScriptedTurn], *, model: str = "fake-model") -> None:
        self.script = list(script)
        self.model = model
        self.requests: list[LlmRequest] = []
        self.started = asyncio.Event()
        self.calls = 0
        self.closed = False

    def stream(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        self.calls += 1
        if not self.script:
            raise AssertionError("ScriptedProvider has no scripted turn left")
        turn = self.script.pop(0)

        async def generate() -> AsyncIterator[StreamEvent]:
            self.started.set()
            if turn.error is not None:
                raise turn.error
            for index, event in enumerate(turn.events):
                if turn.pause is not None and index == turn.pause_after:
                    await turn.pause.wait()
                yield event

        return generate()

    async def aclose(self) -> None:
        self.closed = True


class FakeTool:
    """A Tool with recorded calls and a configurable result."""

    def __init__(
        self,
        name: str,
        *,
        text: str = "ok",
        is_error: bool = False,
        parallel_safe: bool = True,
        delay: float = 0.0,
        error: BaseException | None = None,
        description: str = "",
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = description or f"{name} tool"
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.parallel_safe = parallel_safe
        self.text = text
        self.is_error = is_error
        self.delay = delay
        self.error = error
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append((dict(args), ctx))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return ToolResult(
            text=self.text.format(**args) if "{" in self.text else self.text, is_error=self.is_error
        )


class FakeRegistry:
    """Minimal ToolRegistry: register/get/schemas."""

    def __init__(self, tools: list[FakeTool] | None = None) -> None:
        self._tools: dict[str, FakeTool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: FakeTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> FakeTool | None:
        return self._tools.get(name)

    def schemas(self) -> list[ToolSchema]:
        return [
            ToolSchema(name=tool.name, description=tool.description, parameters=tool.parameters)
            for tool in self._tools.values()
        ]

    def names(self) -> list[str]:
        return list(self._tools)

    def without(self, names: Any) -> FakeRegistry:
        excluded = set(names)
        return FakeRegistry([tool for name, tool in self._tools.items() if name not in excluded])


class RecordingSink:
    """An EventSink that remembers events and the log length at emit time."""

    def __init__(self, session: FakeSessionLog | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.session = session
        self.record_counts: list[int] = []

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self.record_counts.append(len(self.session.records) if self.session is not None else 0)

    def types(self) -> list[str]:
        return [str(event.get("type")) for event in self.events]

    def of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("type") == event_type]


class FakePermission:
    """Structural stand-in for deeppyro.sandbox.PermissionGate."""

    def __init__(self, preset: str = "workspace-write", approval: str = "ask") -> None:
        self.preset = preset
        self.approval = approval


@dataclass
class FakeServices:
    """Arbitrary service bag used to verify ToolContext.services wiring."""

    values: dict[str, Any] = field(default_factory=dict)


def usage(input_tokens: int = 10, output_tokens: int = 5, cached: int | None = 0) -> Any:
    from deeppyro.core.protocols import Usage

    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cached_input_tokens=cached,
    )


def finish(kind: str = "stop", failure: dict[str, Any] | None = None) -> Finish:
    return Finish(kind=kind, failure=failure)
