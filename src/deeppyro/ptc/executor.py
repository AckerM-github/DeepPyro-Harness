"""Nested tool dispatch for programmatic tool calling.

A PTC program calls tools through async bindings.  Every binding call runs the
ordinary tool pipeline (hooks, argument decoding, output retention) but is
*not* a model-visible tool call: it is recorded as tool/ptc-dispatch-start and
tool/ptc-dispatch, and its result travels back to the worker as a binding value.

Permission denials, unknown tools, and tool errors are converted to
PtcBindingError, which the worker raises as ToolCallError so the program may
catch and handle them.  Quota exhaustion is fatal for the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.pipeline import ToolCallRequest, ToolOutcome, run_tool_call

from .types import PtcBindingError, PtcFailure

#: Tools that must never be reachable through the nested binding transport.
NESTED_EXCLUDED_TOOLS = frozenset({"run_code", "ask_user_question"})

_QUOTA_CODE = "QUOTA_EXCEEDED"
_UNKNOWN_CODE = "UNKNOWN_TOOL"


class NestedDispatcher:
    """Turn the session's tool registry into async bindings for one program.

    Concurrency mirrors the model-facing pipeline: parallel-safe tools may run
    concurrently up to max_parallel_sub_calls; a non-parallel-safe tool is an
    exclusive barrier.  The total number of nested calls is bounded by the
    timeout-derived quota and by a rolling one-minute window.
    """

    def __init__(
        self,
        *,
        root_call_id: str,
        registry: Any,
        ctx: ToolContext,
        hooks: Any | None = None,
        max_parallel_sub_calls: int = 10,
        max_sub_calls_per_minute: int = 1024,
        timeout_ms: int = 120_000,
    ) -> None:
        if max_parallel_sub_calls < 1:
            raise ValueError("max_parallel_sub_calls must be at least 1")
        if max_sub_calls_per_minute < 1:
            raise ValueError("max_sub_calls_per_minute must be at least 1")
        if timeout_ms < 1:
            raise ValueError("timeout_ms must be at least 1")
        self.root_call_id = root_call_id
        self.registry = registry
        self.ctx = ctx
        self.hooks = hooks
        self.max_parallel_sub_calls = max_parallel_sub_calls
        self.max_sub_calls_per_minute = max_sub_calls_per_minute
        self.timeout_ms = timeout_ms
        self.max_total_sub_calls = max(1, math.floor(timeout_ms / 60_000 * max_sub_calls_per_minute))
        self._sequence = itertools.count(1)
        self._quota_lock = asyncio.Lock()
        self._recent_calls: list[float] = []
        self._call_count = 0
        self._fatal: PtcFailure | None = None
        self._deferred: list[dict[str, Any]] = []
        self._condition = asyncio.Condition()
        self._active_parallel = 0
        self._exclusive = False
        self._parallel_slots = asyncio.Semaphore(max_parallel_sub_calls)
        self._bindings: dict[str, Callable[..., Any]] = {
            name: self._make_binding(name)
            for name in self._registry_names(registry)
            if name not in NESTED_EXCLUDED_TOOLS
        }

    @staticmethod
    def _registry_names(registry: Any) -> list[str]:
        names = getattr(registry, "names", None)
        if not callable(names):
            return []
        try:
            return [str(name) for name in names()]
        except Exception:  # noqa: BLE001 - a broken registry simply yields no bindings
            return []

    @property
    def bindings(self) -> dict[str, Callable[..., Any]]:
        """The worker-facing binding map (tool name -> async callable)."""
        return dict(self._bindings)

    @property
    def fatal(self) -> PtcFailure | None:
        """A run-fatal failure such as quota exhaustion, or None."""
        return self._fatal

    @property
    def deferred_parts(self) -> list[dict[str, Any]]:
        """Image parts that run_code must attach after its tool message."""
        return list(self._deferred)

    #: Alias used by callers that think of the parts as deferred images.
    @property
    def deferred_images(self) -> list[dict[str, Any]]:
        return self.deferred_parts

    @property
    def call_count(self) -> int:
        """How many nested calls were admitted for this program."""
        return self._call_count

    def _make_binding(self, name: str) -> Callable[..., Any]:
        async def binding(arguments: Mapping[str, Any] | None = None, **kwargs: Any) -> str:
            if arguments is None:
                payload: dict[str, Any] = dict(kwargs)
            elif isinstance(arguments, Mapping):
                payload = dict(arguments)
                payload.update(kwargs)
            else:
                raise PtcBindingError(name, "INVALID_ARGUMENTS", f"{name}() expects a JSON object")
            outcome = await self.dispatch(name, payload)
            return outcome.result.text

        binding.__name__ = name
        return binding

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Execute one nested call and record its start/settle events."""
        if name in NESTED_EXCLUDED_TOOLS:
            raise PtcBindingError(name, _UNKNOWN_CODE, f'unknown tool "{name}"')
        if self._fatal is not None:
            raise PtcBindingError(name, _QUOTA_CODE, self._fatal.message)
        await self._admit_call(name)
        sub_call_id = f"{self.root_call_id}:ptc:{next(self._sequence)}"
        tool = self.registry.get(name) if hasattr(self.registry, "get") else None
        parallel = True if tool is None else bool(getattr(tool, "parallel_safe", True))
        self._log(
            "tool/ptc-dispatch-start",
            {
                "rootCallId": self.root_call_id,
                "parentCallId": self.root_call_id,
                "subCallId": sub_call_id,
                "name": name,
                "arguments": dict(arguments),
            },
        )
        error: dict[str, str] | None = None
        outcome: ToolOutcome | None = None
        try:
            if parallel:
                await self._parallel_slots.acquire()
                try:
                    await self._enter_parallel()
                    try:
                        outcome = await self._invoke(sub_call_id, name, arguments)
                    finally:
                        await self._exit_parallel()
                finally:
                    self._parallel_slots.release()
            else:
                await self._enter_exclusive()
                try:
                    outcome = await self._invoke(sub_call_id, name, arguments)
                finally:
                    await self._exit_exclusive()
        except asyncio.CancelledError:
            error = {
                "name": "CancelledError",
                "code": "ABORTED",
                "reason": "the nested tool call was aborted",
            }
            raise
        finally:
            result = outcome.result if outcome is not None else None
            if result is not None and result.is_error:
                error = self._error_info(result)
            self._record_dispatch(sub_call_id, name, arguments, result, error)
        assert outcome is not None
        result = outcome.result
        if result.is_error:
            info = self._error_info(result)
            raise PtcBindingError(name, info["code"], info["reason"])
        self._collect_parts(result)
        return outcome

    async def _invoke(self, sub_call_id: str, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            encoded = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise PtcBindingError(name, "INVALID_ARGUMENTS", f"arguments are not JSON: {exc}") from exc
        request = ToolCallRequest(call_id=sub_call_id, name=name, arguments=encoded)

        def ctx_factory(_request: ToolCallRequest) -> ToolContext:
            return ToolContext(
                cwd=self.ctx.cwd,
                session_id=self.ctx.session_id,
                turn=self.ctx.turn,
                step=self.ctx.step,
                call_id=sub_call_id,
                cancel=self.ctx.cancel,
                log=self.ctx.log,
                permission=self.ctx.permission,
                services=self.ctx.services,
            )

        return await run_tool_call(
            request,
            registry=self.registry,
            ctx_factory=ctx_factory,
            hooks=self.hooks,
            turn=self.ctx.turn,
            step=self.ctx.step,
        )

    async def _admit_call(self, name: str) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        async with self._quota_lock:
            if self._fatal is not None:
                raise PtcBindingError(name, _QUOTA_CODE, self._fatal.message)
            self._recent_calls = [stamp for stamp in self._recent_calls if now - stamp < 60.0]
            exceeded = (
                self._call_count >= self.max_total_sub_calls
                or len(self._recent_calls) >= self.max_sub_calls_per_minute
            )
            if exceeded:
                self._fatal = PtcFailure(
                    "exception",
                    (
                        "quota exceeded: nested tool calls are limited to "
                        f"{self.max_total_sub_calls} per run and "
                        f"{self.max_sub_calls_per_minute} per minute"
                    ),
                )
                raise PtcBindingError(name, _QUOTA_CODE, self._fatal.message)
            self._call_count += 1
            self._recent_calls.append(now)

    async def _enter_parallel(self) -> None:
        async with self._condition:
            while self._exclusive:
                await self._condition.wait()
            self._active_parallel += 1

    async def _exit_parallel(self) -> None:
        async with self._condition:
            self._active_parallel = max(0, self._active_parallel - 1)
            if self._active_parallel == 0:
                self._condition.notify_all()

    async def _enter_exclusive(self) -> None:
        async with self._condition:
            while self._exclusive or self._active_parallel:
                await self._condition.wait()
            self._exclusive = True

    async def _exit_exclusive(self) -> None:
        async with self._condition:
            self._exclusive = False
            self._condition.notify_all()

    @staticmethod
    def _error_info(result: ToolResult) -> dict[str, str]:
        meta = result.meta if isinstance(result.meta, dict) else {}
        code = meta.get("code")
        if not isinstance(code, str) or not code:
            code = "TOOL_ERROR"
        reason = result.text
        reason = reason.removeprefix("Error: ")
        error_class = meta.get("errorClass")
        if not isinstance(error_class, str) or not error_class:
            error_class = "ToolError"
        return {"name": error_class, "code": code, "reason": reason}

    def _record_dispatch(
        self,
        sub_call_id: str,
        name: str,
        arguments: dict[str, Any],
        result: ToolResult | None,
        error: dict[str, str] | None,
    ) -> None:
        payload: dict[str, Any] = {
            "rootCallId": self.root_call_id,
            "parentCallId": self.root_call_id,
            "subCallId": sub_call_id,
            "name": name,
            "arguments": dict(arguments),
            "isError": bool(result.is_error) if result is not None else True,
            "content": [{"type": "text", "text": result.text if result is not None else ""}],
        }
        if result is not None and result.meta:
            payload["meta"] = dict(result.meta)
        if error is not None:
            payload["error"] = dict(error)
        self._log("tool/ptc-dispatch", payload)

    def _collect_parts(self, result: ToolResult) -> None:
        for part in result.parts or []:
            if isinstance(part, Mapping) and part.get("type") == "image":
                self._deferred.append(dict(part))

    def _log(self, kind: str, data: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.ctx.log(kind, data)


__all__ = ["NESTED_EXCLUDED_TOOLS", "NestedDispatcher"]
