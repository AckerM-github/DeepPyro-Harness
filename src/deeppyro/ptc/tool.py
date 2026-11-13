"""The run_code tool: the model-facing entry point of the Python PTC mode."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from deeppyro.core.errors import AbortedError, PermissionDenied, ToolError
from deeppyro.core.ids import new_call_id
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.args import optional_int, require_str
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.permissions import gate_for

from .runtime import PtcRuntime
from .types import PtcBindingError

RUN_CODE_NAME = "run_code"
#: Tools that must not be callable through the nested binding transport.
NESTED_EXCLUDED_TOOLS = frozenset({RUN_CODE_NAME, "ask_user_question"})


class RunCodeTool(BaseTool):
    """Execute a Python program with async bindings to the session's tools."""

    name = RUN_CODE_NAME
    description = (
        "Run a Python program in a fresh isolated Python process. The program is an async function "
        "body: use await to call the session's available tools by name, and return a JSON value. "
        "Print output is captured. Use this to combine tool calls and process their results in code."
    )
    parameters: ClassVar[dict[str, Any]] = make_parameters(
        {
            "program": {
                "type": "string",
                "description": "Python source. The function body may use top-level await and return.",
            },
            "timeoutMs": {
                "type": "integer",
                "description": "Timeout for the whole program, including nested tool calls.",
                "minimum": 1,
            },
        },
        required=["program"],
        additional_properties=False,
    )
    parallel_safe = False
    reserved_tool = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        program = require_str(args, "program", allow_empty=True)
        gate = gate_for(ctx)
        if getattr(gate, "preset", "") == "read-only":
            return ToolResult(
                text="run_code is unavailable under read-only policy",
                is_error=True,
                meta={"kind": "sandbox-unavailable"},
            )
        runtime = (ctx.services or {}).get("ptc")
        if runtime is None:
            raise ToolError("PTC runtime is not available in this session")
        if not isinstance(runtime, PtcRuntime) and not hasattr(runtime, "run"):
            raise ToolError("PTC runtime is not available in this session")
        config = getattr(runtime, "config", None)
        default_timeout = getattr(config, "timeout_ms", 120_000)
        max_timeout = getattr(config, "max_timeout_ms", 600_000)
        timeout_ms = optional_int(
            args,
            "timeoutMs",
            default_timeout,
            minimum=1,
            maximum=max_timeout,
        )
        registry = (ctx.services or {}).get("tool_registry")
        bindings: dict[str, Any] = {}
        if registry is not None:
            for name in registry.names():
                if name in NESTED_EXCLUDED_TOOLS:
                    continue
                bindings[name] = self._binding(name, registry, ctx)
        request = {"program": program, "timeoutMs": timeout_ms}
        try:
            spec = runtime.resolve(request, cwd=ctx.cwd)
        except Exception as exc:
            # resolve() reports configuration errors; surface them as tool errors.
            raise ToolError(f"invalid run_code arguments: {exc}") from exc
        result = await runtime.run(spec, bindings=bindings, ctx=ctx)
        logs = "".join(result.logs).rstrip("\n")
        if result.failure is not None:
            text = f"Error: code run failed ({result.failure.kind}): {result.failure.message}"
            if logs:
                text += "\n\nCaptured output:\n" + logs
            return ToolResult(
                text=text,
                is_error=True,
                meta={
                    "kind": result.failure.kind,
                    "sandbox": dict(result.sandbox),
                    "logs": logs,
                },
            )
        parts: list[str] = []
        if logs:
            parts.append(logs)
        if result.value is not None:
            parts.append(json.dumps(result.value, ensure_ascii=False))
        text = "\n\n".join(parts) if parts else "(run_code completed with no output)"
        return ToolResult(
            text=text,
            meta={
                "sandbox": dict(result.sandbox),
                "logs": logs,
                "value": result.value,
            },
        )

    @staticmethod
    def _binding(name: str, registry: Any, parent: ToolContext) -> Any:
        async def binding(arguments: dict[str, Any]) -> str:
            return await RunCodeTool._dispatch(name, arguments, registry, parent)

        binding.__name__ = name
        return binding

    @staticmethod
    async def _dispatch(name: str, arguments: dict[str, Any], registry: Any, parent: ToolContext) -> str:
        sub_call_id = new_call_id()
        parent.log(
            "tool/ptc-dispatch-start",
            {
                "rootCallId": parent.call_id,
                "parentCallId": parent.call_id,
                "subCallId": sub_call_id,
                "name": name,
                "arguments": dict(arguments),
            },
        )
        result: ToolResult | None = None
        error_info: dict[str, str] | None = None
        try:
            if parent.cancel.is_set():
                raise AbortedError("run_code was aborted")
            tool = registry.get(name)
            if tool is None:
                raise ToolError(f'unknown tool "{name}"')
            sub_context = ToolContext(
                cwd=parent.cwd,
                session_id=parent.session_id,
                turn=parent.turn,
                step=parent.step,
                call_id=sub_call_id,
                cancel=parent.cancel,
                log=parent.log,
                permission=parent.permission,
                services=parent.services,
            )
            result = await tool.run(arguments, sub_context)
            if not isinstance(result, ToolResult):
                raise ToolError(f"tool {name!r} returned an invalid result")
        except Exception as exc:  # noqa: BLE001 - every nested failure becomes a structured result
            code = "PERMISSION_DENIED" if isinstance(exc, PermissionDenied) else "TOOL_ERROR"
            error_info = {"name": type(exc).__name__, "code": code, "reason": str(exc)}
            result = ToolResult(text=f"Error: {exc}", is_error=True, meta={"error": error_info})
        assert result is not None
        dispatch: dict[str, Any] = {
            "rootCallId": parent.call_id,
            "parentCallId": parent.call_id,
            "subCallId": sub_call_id,
            "name": name,
            "arguments": dict(arguments),
            "isError": result.is_error,
            "content": [{"type": "text", "text": result.text}],
        }
        if result.meta:
            dispatch["meta"] = dict(result.meta)
        if error_info is not None:
            dispatch["error"] = error_info
        parent.log("tool/ptc-dispatch", dispatch)
        if result.is_error:
            reason = result.text
            reason = reason.removeprefix("Error: ")
            code = (
                "PERMISSION_DENIED"
                if error_info and error_info.get("code") == "PERMISSION_DENIED"
                else "TOOL_ERROR"
            )
            raise PtcBindingError(name, code, reason)
        return result.text


__all__ = ["NESTED_EXCLUDED_TOOLS", "RUN_CODE_NAME", "RunCodeTool"]
