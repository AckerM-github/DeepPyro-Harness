"""Tool batch execution: rolling concurrency, barriers, ordering, retention, hooks.

Semantics (frozen by docs/spec/INTERFACES.md section 6, extended by task-29):

* parallel_safe tools may run concurrently, up to max_concurrency.
* A non-parallel-safe tool is a barrier: every earlier call finishes first,
  the tool runs alone, and only then may later calls start.
* Outcomes come back in request order, not completion order.
* Unknown tools, malformed arguments, permission denials, tool errors, and
  unexpected exceptions all become ToolResult(is_error=True); only
  cancellation propagates.

The optional hooks object is duck-typed: when it exposes an async
waterfall(name, payload, fallback) method, the pipeline calls the DSH events
tools/pre-execute, tools/execute, and tools/post-execute. Hook failures are
normalized like tool failures, except cancellation. The optional log callback
is invoked once per request, in request order, before any pre-execute hook, so
that a caller can append the tool/call record and return its event seq. That
seq is exposed on ToolOutcome.call_seq and is intended for
tool/result.sourceEventSeqs. Once every call record is durable, the pipeline
emits the tool/call hook for each recorded call before any tool runs, so
listeners observe exactly the calls whose records exist.

Post-execute accept semantics: content replaces ToolResult.text; value is
carried as ToolResult.meta["value"] because the frozen core ToolResult has no
canonical value field. Replacing the value of a failed result is a policy
error (ValueError internally, surfaced as an error result).
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import time
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from deeppyro.core.errors import AbortedError, PermissionDenied, ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.hooks import TOOL_CALL, TOOLS_EXECUTE, TOOLS_POST_EXECUTE, TOOLS_PRE_EXECUTE
from deeppyro.tools.registry import ToolRegistry
from deeppyro.tools.spill import (
    default_spill_dir,
    resolve_limit,
    spill_dir_from_services,
    spill_text,
    truncate_text,
)

Fallback = Callable[..., Any]


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
    call_seq: int | None = None


def _error_result(message: str, *, meta: dict[str, Any] | None = None) -> ToolResult:
    return ToolResult(text=f"Error: {message}", is_error=True, meta=meta)


def _error_meta(exc: BaseException, code: str) -> dict[str, str]:
    """Structured metadata for a nested call failure (not shown to the model)."""
    return {"code": code, "errorClass": type(exc).__name__}


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
    return ToolResult(
        text=kept,
        is_error=result.is_error,
        meta=meta,
        parts=list(result.parts or []),
        error=result.error,
    )


def _call_payload(request: ToolCallRequest) -> dict[str, Any]:
    """The wire-shaped tool call shared by every hook payload."""
    return {"callId": request.call_id, "name": request.name, "arguments": request.arguments}


def _hook_fields(agent: Any | None, tool_set: Sequence[str] | None) -> dict[str, Any]:
    """Context fields added to every tool hook payload.

    agent is always present (None when the caller has no agent object); toolSet
    appears only when the caller supplies the direct tool name snapshot.
    """
    fields: dict[str, Any] = {"agent": agent}
    if tool_set is not None:
        fields["toolSet"] = list(tool_set)
    return fields


def _hook_scope(agent: Any | None) -> dict[str, Any] | None:
    """Derive the hook dispatch scope from the agent that owns the call.

    Sessions expose their durable id; preset is attached by the agents service
    (and may be absent for bare agents), so it is only added when present.
    """
    if agent is None:
        return None
    session_id = getattr(agent, "session_id", None)
    if not isinstance(session_id, str) or not session_id:
        return None
    scope: dict[str, Any] = {"session": session_id}
    preset = getattr(agent, "preset", None)
    if isinstance(preset, str) and preset:
        scope["preset"] = preset
    return scope


def _scoped(
    dispatch: Callable[..., Any],
    name: str,
    *args: Any,
    scope: dict[str, Any] | None,
) -> Any:
    """Call a hook dispatcher with a dispatch scope when it supports one."""
    if scope is None:
        return dispatch(name, *args)
    try:
        return dispatch(name, *args, scope=scope)
    except TypeError:
        # Minimal duck-typed hook objects (tests, embedders) predate scopes.
        return dispatch(name, *args)


def _decision_reason(decision: dict[str, Any], default: str) -> str:
    reason = decision.get("reason")
    if reason is None:
        return default
    text = str(reason).strip()
    return text or default


def _content_to_text(content: Any) -> str:
    """Flatten hook content blocks into the single text channel of ToolResult."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(content, ensure_ascii=False, default=str)
    if isinstance(content, (list, tuple)):
        rendered: list[str] = []
        for block in content:
            if isinstance(block, str):
                rendered.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    rendered.append(text)
                else:
                    rendered.append(json.dumps(block, ensure_ascii=False, default=str))
            else:
                rendered.append(str(block))
        return "\n".join(rendered)
    return str(content)


async def _waterfall(
    hooks: Any,
    name: str,
    payload: dict[str, Any],
    fallback: Fallback,
    *,
    scope: dict[str, Any] | None = None,
) -> Any:
    """Invoke a duck-typed async waterfall, tolerating a sync return value."""
    waterfall = getattr(hooks, "waterfall", None) if hooks is not None else None
    if not callable(waterfall):
        outcome = fallback()
    else:
        outcome = _scoped(waterfall, name, payload, fallback, scope=scope)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    return outcome


async def _pre_execute(
    hooks: Any,
    request: ToolCallRequest,
    call: dict[str, Any],
    turn: int | None,
    step: int | None,
    *,
    agent: Any | None = None,
    tool_set: Sequence[str] | None = None,
) -> ToolResult | None:
    """Run tools/pre-execute; return a denial result, or None to continue."""
    payload = {
        "call": call,
        "tool": request.name,
        "turn": turn,
        "step": step,
        **_hook_fields(agent, tool_set),
    }

    async def fallback(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"kind": "allow"}

    decision = await _waterfall(hooks, TOOLS_PRE_EXECUTE, payload, fallback, scope=_hook_scope(agent))
    if not isinstance(decision, dict):
        raise ToolError(f"tools/pre-execute must return a decision object, not {type(decision).__name__}")
    kind = decision.get("kind")
    if kind == "allow":
        return None
    if kind == "deny":
        return _error_result(_decision_reason(decision, "tool execution denied"))
    if kind == "ask":
        reason = _decision_reason(decision, "")
        message = f"tool {request.name!r} requires approval, but no approval mechanism is available"
        if reason:
            message = f"{message}: {reason}"
        return _error_result(message)
    if kind == "cancel":
        reason = _decision_reason(decision, "")
        message = "tool execution was cancelled"
        if reason:
            message = f"{message}: {reason}"
        return _error_result(message)
    raise ToolError(f"invalid tools/pre-execute decision kind: {kind!r}")


async def _execute(
    hooks: Any,
    tool: Any,
    args: dict[str, Any],
    ctx: ToolContext,
    request: ToolCallRequest,
    call: dict[str, Any],
    turn: int | None,
    step: int | None,
    dispatch: Callable[..., Any] | None,
    *,
    agent: Any | None = None,
    tool_set: Sequence[str] | None = None,
) -> Any:
    """Run tools/execute, falling back to the real tool (or a test dispatch)."""
    payload = {
        "call": call,
        "tool": request.name,
        "turn": turn,
        "step": step,
        "arguments": args,
        **_hook_fields(agent, tool_set),
    }

    async def fallback(*_args: Any, **_kwargs: Any) -> Any:
        if dispatch is None:
            return await tool.run(args, ctx)
        outcome = dispatch(tool, args, ctx)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return outcome

    decision = await _waterfall(hooks, TOOLS_EXECUTE, payload, fallback, scope=_hook_scope(agent))
    if isinstance(decision, ToolResult):
        return decision
    if isinstance(decision, dict):
        if decision.get("kind") == "result" or "result" in decision:
            return decision.get("result")
        raise ToolError(f"invalid tools/execute decision kind: {decision.get('kind')!r}")
    return decision


async def _post_execute(
    hooks: Any,
    request: ToolCallRequest,
    call: dict[str, Any],
    turn: int | None,
    step: int | None,
    result: ToolResult,
    *,
    agent: Any | None = None,
    tool_set: Sequence[str] | None = None,
) -> ToolResult:
    """Run tools/post-execute and apply its accept/block decision."""
    payload = {
        "call": call,
        "tool": request.name,
        "turn": turn,
        "step": step,
        "result": result,
        **_hook_fields(agent, tool_set),
    }

    async def fallback(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"kind": "accept"}

    decision = await _waterfall(hooks, TOOLS_POST_EXECUTE, payload, fallback, scope=_hook_scope(agent))
    if not isinstance(decision, dict):
        raise ToolError(f"tools/post-execute must return a decision object, not {type(decision).__name__}")
    kind = decision.get("kind")
    if kind == "block":
        feedback = decision.get("feedback")
        text = _content_to_text(feedback)
        return ToolResult(
            text=text or "tool result blocked by post-execute policy",
            is_error=True,
            error=result.error,
        )
    if kind != "accept":
        raise ToolError(f"invalid tools/post-execute decision kind: {kind!r}")

    has_content = "content" in decision
    has_value = "value" in decision
    if has_content and has_value:
        raise ValueError("tools/post-execute accept decision cannot replace both value and content")
    if has_value:
        if result.is_error:
            raise ValueError("tools/post-execute cannot replace the value of a failed result")
        meta: dict[str, Any] = dict(result.meta or {})
        meta["value"] = decision["value"]
        return ToolResult(
            text=result.text,
            is_error=False,
            meta=meta,
            parts=list(result.parts or []),
            error=result.error,
        )
    if has_content:
        return ToolResult(
            text=_content_to_text(decision["content"]),
            is_error=result.is_error,
            meta=result.meta,
            parts=list(result.parts or []),
            error=result.error,
        )
    return result


async def _run_hooked(
    hooks: Any,
    tool: Any,
    args: dict[str, Any],
    ctx: ToolContext,
    request: ToolCallRequest,
    call: dict[str, Any],
    turn: int | None,
    step: int | None,
    dispatch: Callable[..., Any] | None,
    *,
    agent: Any | None = None,
    tool_set: Sequence[str] | None = None,
) -> ToolResult:
    """Run pre/execute/post around one tool, applying DSH decision semantics."""
    try:
        denied = await _pre_execute(hooks, request, call, turn, step, agent=agent, tool_set=tool_set)
        if denied is not None:
            return denied
        raw = await _execute(
            hooks,
            tool,
            args,
            ctx,
            request,
            call,
            turn,
            step,
            dispatch,
            agent=agent,
            tool_set=tool_set,
        )
        if not isinstance(raw, ToolResult):
            raise ToolError(f"tool {request.name!r} returned {type(raw).__name__}, not ToolResult")
        return await _post_execute(hooks, request, call, turn, step, raw, agent=agent, tool_set=tool_set)
    except ValueError as exc:
        # Policy violations are ordinary error results, not turn-fatal exceptions.
        raise ToolError(str(exc)) from exc


def _collect_call_seqs(
    requests: list[ToolCallRequest],
    *,
    log: Callable[..., int] | None,
    turn: int | None,
    step: int | None,
) -> tuple[list[int | None], dict[int, ToolResult]]:
    """Append tool/call records in request order and capture their seqs."""
    seqs: list[int | None] = [None] * len(requests)
    failures: dict[int, ToolResult] = {}
    if log is None:
        return seqs, failures
    for index, request in enumerate(requests):
        try:
            seq = log(turn, step, _call_payload(request))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - recording failure must not kill the turn
            failures[index] = _error_result(
                f"cannot record tool call: {exc}",
                meta={"traceback": traceback.format_exc()},
            )
            continue
        if isinstance(seq, int) and not isinstance(seq, bool):
            seqs[index] = seq
        else:
            failures[index] = _error_result(
                f"log() must return an integer tool/call seq, not {type(seq).__name__}"
            )
    return seqs, failures


async def _emit_call_events(
    hooks: Any,
    requests: list[ToolCallRequest],
    call_seqs: list[int | None],
    *,
    turn: int | None,
    step: int | None,
    agent: Any | None,
) -> None:
    """Emit tool/call for every call whose record was appended, before dispatch."""
    emit = getattr(hooks, "emit", None) if hooks is not None else None
    if not callable(emit):
        return
    has = getattr(hooks, "has", None)
    observed = bool(getattr(hooks, "observed", False))
    if callable(has) and not (has(TOOL_CALL) or observed):
        return
    scope = _hook_scope(agent)
    for request, seq in zip(requests, call_seqs):
        if seq is None:
            continue
        outcome = _scoped(
            emit,
            TOOL_CALL,
            {
                "agent": agent,
                "callId": request.call_id,
                "name": request.name,
                "turn": turn,
                "step": step,
            },
            scope=scope,
        )
        if inspect.isawaitable(outcome):
            await outcome


async def run_tool_call(
    request: ToolCallRequest,
    *,
    registry: ToolRegistry,
    ctx_factory: Callable[[ToolCallRequest], ToolContext],
    hooks: Any | None = None,
    turn: int | None = None,
    step: int | None = None,
    agent: Any | None = None,
    tool_set: Sequence[str] | None = None,
) -> ToolOutcome:
    """Run exactly one tool call through the normal pre/execute/post pipeline.

    This is the single-call entry point used by nested transports (PTC).  It
    does not append a tool/call record and never raises for tool failures:
    unknown tools, malformed arguments, permission denials, and tool exceptions
    are all converted to an error result.  Only cancellation propagates.
    """
    ctx = ctx_factory(request)
    started = time.monotonic()
    call = _call_payload(request)
    tool = registry.get(request.name)
    result: ToolResult
    if tool is None:
        result = _error_result(f'unknown tool "{request.name}"', meta={"code": "UNKNOWN_TOOL"})
    else:
        try:
            args = _decode_arguments(request.arguments, request.name)
            if hooks is None:
                result = await tool.run(args, ctx)
                if not isinstance(result, ToolResult):
                    raise ToolError(f"tool {request.name!r} returned {type(result).__name__}, not ToolResult")
            else:
                result = await _run_hooked(
                    hooks,
                    tool,
                    args,
                    ctx,
                    request,
                    call,
                    turn,
                    step,
                    None,
                    agent=agent,
                    tool_set=tool_set,
                )
        except asyncio.CancelledError:
            raise
        except PermissionDenied as exc:
            result = _error_result(str(exc), meta=_error_meta(exc, "PERMISSION_DENIED"))
        except AbortedError as exc:
            result = _error_result(str(exc), meta=_error_meta(exc, "ABORTED"))
        except ToolError as exc:
            result = _error_result(str(exc), meta=_error_meta(exc, "TOOL_ERROR"))
        except Exception as exc:  # noqa: BLE001 - a nested tool bug must not kill the program
            result = _error_result(
                f"tool {request.name!r} failed: {exc}",
                meta={"code": "TOOL_ERROR", "traceback": traceback.format_exc()},
            )
    return ToolOutcome(
        call_id=request.call_id,
        name=request.name,
        result=_retain_output(result, ctx, request.name),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def run_tool_batch(
    requests: list[ToolCallRequest],
    *,
    registry: ToolRegistry,
    ctx_factory: Callable[[ToolCallRequest], ToolContext],
    max_concurrency: int = 8,
    hooks: Any | None = None,
    turn: int | None = None,
    step: int | None = None,
    log: Callable[..., int] | None = None,
    dispatch: Callable[..., Any] | None = None,
    agent: Any | None = None,
    tool_set: Sequence[str] | None = None,
) -> list[ToolOutcome]:
    """Execute requests honouring barriers, concurrency, ordering, and hooks."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be at least 1")
    semaphore = asyncio.Semaphore(max_concurrency)
    outcomes: list[ToolOutcome | None] = [None] * len(requests)
    inflight: list[asyncio.Task[None]] = []
    call_seqs, log_failures = _collect_call_seqs(requests, log=log, turn=turn, step=step)
    await _emit_call_events(hooks, requests, call_seqs, turn=turn, step=step, agent=agent)

    async def run_one(index: int, request: ToolCallRequest) -> None:
        ctx = ctx_factory(request)
        started = time.monotonic()
        call = _call_payload(request)
        result: ToolResult
        if index in log_failures:
            result = log_failures[index]
        else:
            tool = registry.get(request.name)
            if tool is None:
                result = _error_result(f'unknown tool "{request.name}"')
            else:
                try:
                    async with semaphore:
                        args = _decode_arguments(request.arguments, request.name)
                        if hooks is None and dispatch is None:
                            result = await tool.run(args, ctx)
                            if not isinstance(result, ToolResult):
                                raise ToolError(
                                    f"tool {request.name!r} returned {type(result).__name__}, not ToolResult"
                                )
                        else:
                            result = await _run_hooked(
                                hooks,
                                tool,
                                args,
                                ctx,
                                request,
                                call,
                                turn,
                                step,
                                dispatch,
                                agent=agent,
                                tool_set=tool_set,
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
            call_seq=call_seqs[index],
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
