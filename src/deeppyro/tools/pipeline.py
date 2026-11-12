"""Tool batch execution: rolling concurrency, barriers, ordering, retention.

Semantics (frozen by `docs/spec/INTERFACES.md` section 6):

* `parallel_safe` tools may run concurrently, up to `max_concurrency`.
* A non-parallel-safe tool is a barrier: every earlier call finishes first,
  the tool runs alone, and only then may later calls start.
* Outcomes come back in request order, not completion order.
* Unknown tools, malformed arguments, permission denials, tool errors, and
  unexpected exceptions all become `ToolResult(is_error=True)`; only
  cancellation propagates.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deeppyro.core.errors import AbortedError, PermissionDenied, ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.registry import ToolRegistry
from deeppyro.tools.spill import (
    default_spill_dir,
    resolve_limit,
    spill_dir_from_services,
    spill_text,
    truncate_text,
)


@dataclasses.dataclass
class ToolCallRequest:
    """One model-requested tool call, before validation or execution."""

    call_id: str
    name: str
    arguments: str


@dataclasses.dataclass
class ToolOutcome:
    """The final result of one tool call, aligned with its request."""

    call_id: str
    name: str
    result: ToolResult
    duration_ms: int


def _error_result(message: str, *, meta: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(text=f"Error: {message}", is_error=True, meta=meta)


def _decode_arguments(raw: str, name: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw) if raw and raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise ToolError(f"invalid JSON arguments for tool {name!r}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ToolError(f"arguments for tool {name!r} must be a JSON object")
    return decoded


def _retain_output(result: ToolResult, ctx: ToolContext, tool_name: str) -> ToolResult:
    """Apply the inline character budget and spill the complete output."""
    services = ctx.services if isinstance(ctx.services, dict) else {}
    text = result.text if isinstance(result.text, str) else str(result.text)
    limit = resolve_limit(services)
    kept, omitted = truncate_text(text, limit)
    meta: dict[str, Any] = dict(result.meta or {})
    if omitted:
        meta["truncated"] = True
        meta["omittedChars"] = omitted
        spill_dir = spill_dir_from_services(services) or default_spill_dir(Path(ctx.cwd))
        spill_path = spill_text(spill_dir, ctx.call_id, tool_name, text)
        meta["spillPath"] = spill_path
    return ToolResult(text=kept, is_error=result.is_error, meta=meta, parts=list(result.parts or []))


async def run_tool_batch(
    requests: list[ToolCallRequest],
    *,
    registry: ToolRegistry,
    ctx_factory: Callable[[ToolCallRequest], ToolContext],
    max_concurrency: int = 8,
) -> list[ToolOutcome]:
    """Execute `requests` honouring barriers, concurrency, and ordering."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    semaphore = asyncio.Semaphore(max_concurrency)
    outcomes: list[ToolOutcome | None] = [None] * len(requests)
    inflight: list[asyncio.Task[None]] = []

    async def run_one(index: int, request: ToolCallRequest) -> None:
        ctx = ctx_factory(request)
        started = time.monotonic()
        tool = registry.get(request.name)
        if tool is None:
            result = _error_result(f'unknown tool "{request.name}"')
        else:
            try:
                async with semaphore:
                    args = _decode_arguments(request.arguments, request.name)
                    result = await tool.run(args, ctx)
                    if not isinstance(result, ToolResult):
                        raise ToolError(
                            f"tool {request.name!r} returned {type(result).__name__}, not ToolResult"
                        )
            except asyncio.CancelledError:
                raise
            except (ToolError, PermissionDenied, AbortedError) as exc:
                result = _error_result(str(exc))
            except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the turn
                result = _error_result(
                    f"tool {request.name!r} failed: {exc}",
                    meta={"traceback": traceback.format_exc()},
                )
        outcomes[index] = ToolOutcome(
            call_id=request.call_id,
            name=request.name,
            result=_retain_output(result, ctx, request.name),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def drain() -> None:
        if inflight:
            await asyncio.gather(*inflight)
            inflight.clear()

    for index, request in enumerate(requests):
        tool = registry.get(request.name)
        parallel = True if tool is None else bool(tool.parallel_safe)
        if not parallel:
            await drain()
            await run_one(index, request)
        else:
            inflight.append(asyncio.create_task(run_one(index, request)))
    await drain()

    return [outcome for outcome in outcomes if outcome is not None]
