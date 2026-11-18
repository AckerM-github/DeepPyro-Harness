"""Replay helpers for the frozen native recordings (M2 differential lane).

The recordings under `fixtures/native/recordings/` are data-only captures of real
provider traffic. This module parses them without modifying any fixture byte and turns
each recorded SSE stream into a scripted LlmProvider whose events mirror what the
real provider produced. A companion recorded tool returns the exact recorded tool
results, so a differential test can compare the native loop's session events with
the recording.

Only the tests import this module: it is a test-only adapter, not production code.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deeppyro.core.llm import (
    Finish,
    ModelRequest,
    StopReason,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
    UsageEvent,
)

NATIVE_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "native"

# Recorded provider finish reasons map onto the native stop-reason enum.
FINISH_TO_STOP: dict[str, StopReason] = {
    "stop": StopReason.STOP,
    "tool_calls": StopReason.TOOL_CALLS,
    "length": StopReason.LENGTH,
    "error": StopReason.ERROR,
    "aborted": StopReason.ABORTED,
    "content_filter": StopReason.CONTENT_FILTER,
}


@dataclass(frozen=True, slots=True)
class ToolResultRecord:
    """One recorded tool result, matched to a call by tool name."""

    name: str
    content: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class Exchange:
    """One recorded request/response pair."""

    request_id: str
    body: dict[str, Any]
    events: tuple[StreamEvent, ...]
    finish_reason: str
    content: str
    usage: Usage


@dataclass(frozen=True, slots=True)
class Recording:
    """A parsed recording of one case."""

    case_id: str
    path: Path
    exchanges: tuple[Exchange, ...]
    tool_results: tuple[ToolResultRecord, ...]
    tool_calls: tuple[tuple[str, str], ...]
    prompts: tuple[str, ...]
    injected_prompts: tuple[str, ...]

    @property
    def assistant_expected(self) -> tuple[tuple[str, str], ...]:
        """Expected (text, native finish reason) for every assistant message."""
        return tuple((exchange.content, exchange.finish_reason) for exchange in self.exchanges)


def load_manifest() -> dict[str, Any]:
    """Read the frozen native fixture manifest."""
    raw = (NATIVE_FIXTURES / "manifest.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("native fixture manifest must be an object")
    return parsed


def recording_paths() -> list[tuple[str, Path]]:
    """Return (case_id, path) pairs in manifest order."""
    manifest = load_manifest()
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ValueError("native fixture manifest has no cases")
    out: list[tuple[str, Path]] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("native fixture case must be an object")
        case_id = case["case_id"]
        file = case["file"]
        if not isinstance(case_id, str) or not isinstance(file, str):
            raise ValueError("native fixture case fields must be strings")
        out.append((case_id, NATIVE_FIXTURES / file))
    return out


def _usage(raw: dict[str, Any]) -> Usage:
    details = raw.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    return Usage(
        input_tokens=int(raw.get("prompt_tokens", 0)),
        output_tokens=int(raw.get("completion_tokens", 0)),
        total_tokens=int(raw.get("total_tokens", 0)),
        cached_input_tokens=int(cached) if isinstance(cached, int) else None,
    )


def _parse_chunk(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if text.startswith("data:"):
        text = text[len("data:") :].strip()
    if not text or text == "[DONE]":
        return None
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("SSE chunk must be an object")
    return parsed


def _stream_events(chunks: Sequence[dict[str, Any]]) -> tuple[list[StreamEvent], str, dict[int, str]]:
    events: list[StreamEvent] = []
    text_parts: list[str] = []
    calls: dict[int, str] = {}
    for chunk in chunks:
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            text_parts.append(content)
            events.append(TextDelta(text=content))
        fragments = delta.get("tool_calls")
        if isinstance(fragments, list):
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                index = int(fragment.get("index", 0))
                call_id = fragment.get("id")
                function = fragment.get("function")
                name: str | None = None
                arguments: str | None = None
                if isinstance(function, dict):
                    raw_name = function.get("name")
                    raw_arguments = function.get("arguments")
                    name = raw_name if isinstance(raw_name, str) and raw_name else None
                    arguments = raw_arguments if isinstance(raw_arguments, str) else None
                if isinstance(call_id, str) and call_id:
                    calls.setdefault(index, call_id)
                    events.append(ToolCallStart(call_id=call_id, name=name or "", index=index))
                elif name is not None:
                    events.append(ToolCallStart(call_id=calls.get(index, ""), name=name, index=index))
                if arguments:
                    events.append(
                        ToolCallArgsDelta(
                            call_id=calls.get(index, ""),
                            delta=arguments,
                            name=name,
                            index=index,
                        )
                    )
    return events, "".join(text_parts), calls


def load_recording(path: Path) -> Recording:
    """Parse one recording file; validates that the SSE replay matches its end record."""
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    request_ids: list[str] = []
    bodies: dict[str, dict[str, Any]] = {}
    chunks: dict[str, list[dict[str, Any]]] = {}
    ends: dict[str, dict[str, Any]] = {}
    tool_results: list[ToolResultRecord] = []
    tool_calls: list[tuple[str, str]] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        kind = line.get("type")
        if kind == "llm.request":
            request_id = line["request_id"]
            request_ids.append(request_id)
            bodies[request_id] = line["body"]
            chunks[request_id] = []
        elif kind == "llm.sse":
            chunk = _parse_chunk(line["raw"])
            if chunk is not None:
                chunks.setdefault(line["request_id"], []).append(chunk)
        elif kind == "llm.response.end":
            ends[line["request_id"]] = line
        elif kind == "tool.call":
            tool_calls.append((str(line["name"]), str(line["arguments"])))
        elif kind == "tool.result":
            tool_results.append(
                ToolResultRecord(
                    name=str(line["name"]),
                    content=str(line.get("content") or ""),
                    is_error=bool(line.get("is_error")),
                )
            )
    exchanges: list[Exchange] = []
    for request_id in request_ids:
        end = ends.get(request_id)
        if end is None:
            raise ValueError(f"recording {path.name} has no response end for {request_id}")
        events, content, _ = _stream_events(chunks.get(request_id, []))
        finish_raw = str(end.get("finish_reason") or "stop")
        if finish_raw not in FINISH_TO_STOP:
            raise ValueError(f"recording {path.name} has unknown finish reason {finish_raw!r}")
        recorded_content = str(end.get("content") or "")
        if content != recorded_content:
            raise ValueError(
                f"recording {path.name} SSE text {content!r} does not match end content {recorded_content!r}"
            )
        usage_raw = end.get("usage")
        usage = _usage(usage_raw) if isinstance(usage_raw, dict) else Usage()
        events.append(UsageEvent(usage=usage))
        events.append(Finish(stop_reason=FINISH_TO_STOP[finish_raw]))
        exchanges.append(
            Exchange(
                request_id=request_id,
                body=bodies[request_id],
                events=tuple(events),
                finish_reason=finish_raw,
                content=recorded_content,
                usage=usage,
            )
        )
    prompts, injected = _prompts([exchange.body for exchange in exchanges])
    return Recording(
        case_id=str(lines[0].get("case_id") if lines else path.stem),
        path=path,
        exchanges=tuple(exchanges),
        tool_results=tuple(tool_results),
        tool_calls=tuple(tool_calls),
        prompts=tuple(prompts),
        injected_prompts=tuple(injected),
    )


def _prompts(bodies: Sequence[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Split new user messages into top-level turn prompts and mid-turn injections."""
    prompts: list[str] = []
    injected: list[str] = []
    seen: list[str] = []
    for body in bodies:
        messages = body.get("messages")
        if not isinstance(messages, list):
            continue
        users: list[tuple[int, str]] = []
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    users.append((index, content))
        user_texts = [text for _, text in users]
        if user_texts[: len(seen)] != seen:
            raise ValueError("recording user messages are not append-only")
        for index, text in users[len(seen) :]:
            previous_role = None
            if index > 0 and isinstance(messages[index - 1], dict):
                previous_role = messages[index - 1].get("role")
            if previous_role == "tool":
                injected.append(text)
            else:
                prompts.append(text)
        seen = user_texts
    return prompts, injected


class ReplayProvider:
    """Native LlmProvider that replays recorded SSE events and records requests."""

    name = "native-replay"

    def __init__(self, exchanges: Iterable[Exchange]) -> None:
        self._exchanges = list(exchanges)
        self._index = 0
        self.requests: list[ModelRequest] = []
        self.closed = False

    @property
    def consumed(self) -> int:
        """Number of exchanges already served."""
        return self._index

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        if self._index >= len(self._exchanges):
            raise AssertionError("replay provider ran out of recorded responses")
        exchange = self._exchanges[self._index]
        self._index += 1
        for event in exchange.events:
            yield event

    async def aclose(self) -> None:
        self.closed = True


class RecordedTool:
    """Return the recorded tool results for one tool name, in recorded order."""

    parallel_safe = True

    def __init__(self, name: str, results: Sequence[ToolResultRecord]) -> None:
        self.name = name
        self.description = f"Recorded {name} tool"
        self.parameters: dict[str, Any] = {"type": "object", "properties": {}}
        self._results = list(results)
        self._index = 0
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: dict[str, Any], ctx: Any) -> Any:
        from deeppyro.core.protocols import ToolResult

        self.calls.append(dict(args))
        if self._index >= len(self._results):
            raise AssertionError(f"recorded tool {self.name!r} has no result left")
        result = self._results[self._index]
        self._index += 1
        return ToolResult(text=result.content, is_error=result.is_error)


def recording_tools(recording: Recording) -> list[RecordedTool]:
    """Create one recorded tool per recorded tool name."""
    grouped: dict[str, list[ToolResultRecord]] = {}
    for result in recording.tool_results:
        grouped.setdefault(result.name, []).append(result)
    return [RecordedTool(name, results) for name, results in grouped.items()]
