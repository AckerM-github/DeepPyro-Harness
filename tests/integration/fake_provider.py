"""Scripted in-process LLM provider for integration tests (no network).

Usage::

    provider = ScriptedProvider([
        [TextDelta("hi"), Finish("stop")],
        [ToolCallStart("c1", "read_file"), ToolCallArgsDelta("c1", '{"path": "a"}'), Finish("tool-calls")],
    ])
    # each stream() call consumes the next scripted response; extra calls yield
    # a plain "stop" response so loops never hang.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from deeppyro.core.protocols import (
    Finish,
    LlmProvider,
    LlmRequest,
    ReasoningDelta,
    StreamEvent,
    TextDelta,
    ToolCallArgsDelta,
    ToolCallStart,
    Usage,
)


@dataclass
class Response:
    """One scripted assistant response: text plus at most one tool call."""

    text: str = ""
    reasoning: str = ""
    tool_calls: list[tuple[str, str, str]] = field(default_factory=list)  # (id, name, args_json)
    finish: str = "stop"
    usage: Usage | None = None

    def events(self) -> list[StreamEvent]:
        out: list[StreamEvent] = []
        if self.reasoning:
            out.append(ReasoningDelta(self.reasoning))
        if self.text:
            out.append(TextDelta(self.text))
        for call_id, name, args in self.tool_calls:
            out.append(ToolCallStart(call_id, name))
            if args:
                out.append(ToolCallArgsDelta(call_id, args))
        if self.usage is not None:
            out.append(self.usage)
        out.append(Finish(self.finish))
        return out


class ScriptedProvider(LlmProvider):
    """Replays a fixed script of responses; records every request."""

    name = "scripted"

    def __init__(self, responses: list[Response | list[StreamEvent]] | None = None) -> None:
        self._responses: list[list[StreamEvent]] = []
        for item in responses or []:
            self._responses.append(item.events() if isinstance(item, Response) else list(item))
        self.requests: list[LlmRequest] = []
        self.closed = False

    def push(self, response: Response | list[StreamEvent]) -> None:
        self._responses.append(response.events() if isinstance(response, Response) else list(response))

    async def stream(self, request: LlmRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        events = self._responses.pop(0) if self._responses else [Finish("stop")]
        for event in events:
            yield event

    async def aclose(self) -> None:
        self.closed = True


def text_response(text: str, finish: str = "stop") -> Response:
    return Response(text=text, finish=finish)


def tool_response(
    call_id: str,
    name: str,
    args: str,
    *,
    text: str = "",
    finish: str = "tool-calls",
) -> Response:
    return Response(text=text, tool_calls=[(call_id, name, args)], finish=finish)
