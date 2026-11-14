"""Request bookkeeping for the agent loop (DSH request/header semantics).

The loop records the request configuration once per request series and again
whenever it changes.  Tools are part of the header: adding or removing a tool
also appends a developer/message describing the change so the model can see it
without re-reading the whole header.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from deeppyro.core.ids import new_message_id
from deeppyro.core.wire import Message, ToolAdditionPart, ToolRemovalPart


def canonical_tools(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the tool definitions in their registry order."""
    return [tool for tool in tools]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class HeaderPlan:
    """Whether to write request/header, and what to write."""

    write: bool
    reason: str | None
    header: dict[str, Any]
    # True only for initial/resume/change records: the reference loop marks the
    # first request of a new surface series so a reader can group requests.
    starts_series: bool = False


@dataclass(frozen=True, slots=True)
class ToolChange:
    """A tool definition that appeared or disappeared since the last header."""

    additions: tuple[dict[str, Any], ...]
    removals: tuple[dict[str, Any], ...]

    @property
    def changed(self) -> bool:
        return bool(self.additions or self.removals)


def latest_header(records: Sequence[Any]) -> dict[str, Any] | None:
    """Return the header of the most recent request/header record."""
    for record in reversed(list(records)):
        if getattr(record, "type", None) != "request/header":
            continue
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            header = data.get("header")
            if isinstance(header, dict):
                return header
    return None


def latest_header_seq(records: Sequence[Any]) -> int | None:
    """Return the seq of the most recent request/header record."""
    for record in reversed(list(records)):
        if getattr(record, "type", None) == "request/header":
            seq = getattr(record, "seq", None)
            return int(seq) if isinstance(seq, int) else None
    return None


def build_header(
    *,
    provider: str,
    model: str,
    max_tokens: int | None,
    tools: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Build the durable request header for the current configuration."""
    return {
        "config": {
            "provider": provider,
            "model": model,
            "maxTokens": max_tokens,
            "reasoningEffort": None,
        },
        "adapterDefaults": {"reasoningEffort": True, "maxTokens": True},
        "tools": canonical_tools(tools),
    }


def plan_header(
    records: Sequence[Any],
    *,
    provider: str,
    model: str,
    max_tokens: int | None,
    tools: Sequence[dict[str, Any]],
    starts_series: bool,
    logged: bool,
) -> HeaderPlan:
    """Decide whether the current request must write a header.

    The logged flag is True once this loop instance has written a header.  A
    resumed session that already carries a header but has not written one yet
    records resume; a brand-new session records initial.

    The starts_series flag is carried on initial/resume/change records (the
    header record starts a request series), but never on a series record: the
    reference loop writes series purely as a marker and stores no seq for it,
    so the flag would be redundant there.
    """
    header = build_header(provider=provider, model=model, max_tokens=max_tokens, tools=tools)
    baseline = latest_header(records)
    if not logged:
        reason = "initial" if baseline is None else "resume"
        return HeaderPlan(write=True, reason=reason, header=header, starts_series=starts_series)
    if baseline is None or _canonical(baseline) != _canonical(header):
        return HeaderPlan(write=True, reason="change", header=header, starts_series=starts_series)
    if starts_series:
        return HeaderPlan(write=True, reason="series", header=header, starts_series=False)
    return HeaderPlan(write=False, reason=None, header=header, starts_series=False)


def tool_changes(
    previous_header: dict[str, Any] | None,
    tools: Sequence[dict[str, Any]],
) -> ToolChange:
    """Compare the tool names in the previous header with the current tools."""
    previous = previous_header.get("tools") if isinstance(previous_header, dict) else None
    previous_list = previous if isinstance(previous, list) else []
    previous_by_name = {
        tool.get("name"): tool
        for tool in previous_list
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    current_by_name = {
        tool.get("name"): tool
        for tool in canonical_tools(tools)
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    additions = tuple(current_by_name[name] for name in current_by_name if name not in previous_by_name)
    removals = tuple(previous_by_name[name] for name in previous_by_name if name not in current_by_name)
    return ToolChange(additions=additions, removals=removals)


def build_tool_change_message(turn: int, step: int, change: ToolChange) -> Message:
    """Build the developer/message that announces tool additions/removals."""
    del turn, step
    content: list[Any] = [ToolAdditionPart(toolName=tool["name"]) for tool in change.additions]
    content.extend(ToolRemovalPart(toolName=tool["name"]) for tool in change.removals)
    return Message(
        role="developer",
        content=content,
        id=new_message_id(),
        source={"kind": "tool-registry"},
    )


def tool_change_data(
    turn: int,
    step: int,
    change: ToolChange,
    *,
    header_seq: int | None = None,
) -> dict[str, Any] | None:
    """Build the developer/message data, or None when no tool changed."""
    if not change.changed:
        return None
    message = build_tool_change_message(turn, step, change)
    data: dict[str, Any] = {
        "turn": turn,
        "step": step,
        "message": message.model_dump(exclude_none=True),
    }
    if change.additions and header_seq is not None:
        data["headerSeq"] = header_seq
    return data


def latest_context(records: Sequence[Any]) -> dict[str, Any] | None:
    """Return the most recent request/context payload."""
    for record in reversed(list(records)):
        if getattr(record, "type", None) != "request/context":
            continue
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            return data
    return None


#: Fields a request/context record compares; a missing key is distinct from a
#: present null, matching the reference loop's undefined-vs-null check.
CONTEXT_FIELDS: tuple[str, ...] = ("provider", "model", "contextWindow", "systemPromptUpdate")

_MISSING = object()


def _context_view(data: dict[str, Any]) -> dict[str, Any]:
    return {field: data.get(field, _MISSING) for field in CONTEXT_FIELDS}


def context_data(
    records: Sequence[Any],
    *,
    provider: str,
    model: str,
    context_window: int | None,
    system_prompt_update: str | None = "in-history",
) -> dict[str, Any] | None:
    """Build request/context when any tracked field changed, else None.

    Only provider/model/contextWindow/systemPromptUpdate are persisted (the
    reference record has no turn/step position).  Absent optional fields are
    omitted rather than written as null.
    """
    previous = latest_context(records)
    payload: dict[str, Any] = {"provider": provider, "model": model}
    if context_window is not None:
        payload["contextWindow"] = context_window
    if system_prompt_update is not None:
        payload["systemPromptUpdate"] = system_prompt_update
    if previous is not None and _context_view(previous) == _context_view(payload):
        return None
    return payload


__all__ = [
    "CONTEXT_FIELDS",
    "HeaderPlan",
    "ToolChange",
    "build_header",
    "build_tool_change_message",
    "canonical_tools",
    "context_data",
    "latest_context",
    "latest_header",
    "latest_header_seq",
    "plan_header",
    "tool_change_data",
    "tool_changes",
]
