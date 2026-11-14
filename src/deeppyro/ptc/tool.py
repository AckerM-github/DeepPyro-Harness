"""The run_code tool: the model-facing entry point of the Python PTC mode."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.args import optional_int, require_str
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.permissions import gate_for
from deeppyro.tools.registry import ToolRegistry

from .executor import NESTED_EXCLUDED_TOOLS, NestedDispatcher
from .runtime import PtcRuntime
from .types import PtcFailure

RUN_CODE_NAME = "run_code"

_DESCRIPTION_PREFIX = (
    "Execute a Python program against the available tools. Takes two required arguments: "
    "`code`, the program to execute, and `description`, a short summary of what the "
    "program does. Call tools as `await tools.name(args)` per the declarations in the system "
    "prompt. Curate the program output. Image-bearing subtool results are attached after the run."
)
_DEFAULT_EXECUTION_INSTRUCTIONS = (
    "Programs run in a fresh CPython process started with -I -S. The working directory is fixed to "
    "the Session's current directory. Inside a program only tools and ToolCallError are bound; call "
    "a tool as await tools.name(args)."
)
_DESCRIPTION_SUFFIX = (
    " Each program starts in the Session's current directory. Running programs keep their initial directory."
)


def _tool_description(runtime: PtcRuntime | None) -> str:
    instructions = getattr(runtime, "execution_instructions", None)
    if not isinstance(instructions, str) or not instructions:
        instructions = _DEFAULT_EXECUTION_INSTRUCTIONS
    return f"{_DESCRIPTION_PREFIX} {instructions}{_DESCRIPTION_SUFFIX}"


class RunCodeTool(BaseTool):
    """Execute a Python program with async bindings to the session's tools."""

    name = RUN_CODE_NAME
    parameters: ClassVar[dict[str, Any]] = {}
    parallel_safe = False
    reserved_tool = True

    def __init__(self, runtime: PtcRuntime | None = None) -> None:
        self.runtime = runtime
        self.description = _tool_description(runtime)
        properties: dict[str, Any] = {
            "code": {
                "type": "string",
                "description": "The Python program to execute. Top-level await and return are supported.",
            },
            "description": {
                "type": "string",
                "description": "A short summary of what the program does.",
            },
        }
        timeout = getattr(runtime, "timeout", None)
        if timeout:
            properties["timeoutMs"] = {
                "type": "number",
                "description": "Timeout for the whole program, including nested tool calls.",
                "minimum": 1,
                "maximum": timeout.get("maxMs"),
            }
        self.parameters = make_parameters(
            properties,
            required=["code", "description"],
            additional_properties=False,
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = require_str(args, "code", allow_empty=True)
        description = require_str(args, "description", allow_empty=True)
        if not description.strip():
            raise ToolError("invalid description: expected a non-empty string")
        gate = gate_for(ctx)
        if getattr(gate, "preset", "") == "read-only":
            return ToolResult(
                text="run_code is unavailable under read-only policy",
                is_error=True,
                meta={"kind": "sandbox-unavailable"},
            )
        runtime = self._runtime(ctx)
        config = getattr(runtime, "config", None)
        default_timeout = getattr(config, "timeout_ms", 120_000)
        max_timeout = getattr(config, "max_timeout_ms", 600_000)
        timeout_ms = optional_int(args, "timeoutMs", default_timeout, minimum=1, maximum=max_timeout)
        registry = (ctx.services or {}).get("tool_registry")
        if registry is None:
            registry = ToolRegistry()
        dispatcher = NestedDispatcher(
            root_call_id=ctx.call_id,
            registry=registry,
            ctx=ctx,
            hooks=(ctx.services or {}).get("hooks"),
            max_parallel_sub_calls=getattr(config, "max_parallel_sub_calls", 10),
            max_sub_calls_per_minute=getattr(config, "max_sub_calls_per_minute", 1024),
            timeout_ms=timeout_ms,
        )
        try:
            spec = runtime.resolve({"program": code, "timeoutMs": timeout_ms}, cwd=ctx.cwd)
        except Exception as exc:  # resolve() reports configuration errors
            raise ToolError(f"invalid run_code arguments: {exc}") from exc
        result = await runtime.run(spec, bindings=dispatcher.bindings, ctx=ctx)
        logs = "".join(result.logs).rstrip("\n")
        failure = dispatcher.fatal or result.failure
        if failure is not None:
            return self._failure_result(failure, logs, result.sandbox)
        rendered = self._success_text(logs, result.value)
        meta: dict[str, Any] = {
            "logs": logs,
            "sandbox": dict(result.sandbox),
        }
        if result.value is not None:
            meta["result"] = result.value
        return ToolResult(
            text=rendered,
            meta=meta,
            parts=list(dispatcher.deferred_parts),
        )

    def _runtime(self, ctx: ToolContext) -> PtcRuntime:
        runtime = self.runtime or (ctx.services or {}).get("ptc")
        if runtime is None or not hasattr(runtime, "run"):
            raise ToolError("PTC runtime is not available in this session")
        return runtime

    @staticmethod
    def _success_text(logs: str, value: Any) -> str:
        pieces: list[str] = []
        if logs:
            pieces.append(logs)
        if value is not None:
            if isinstance(value, str):
                pieces.append(value)
            else:
                pieces.append(json.dumps(value, indent=2, ensure_ascii=False))
        return "\n\n".join(pieces) if pieces else "(run_code completed with no output)"

    @staticmethod
    def _failure_result(failure: PtcFailure, logs: str, sandbox: dict[str, Any]) -> ToolResult:
        text = f"Error: code run failed ({failure.kind}): {failure.message}"
        if logs:
            text += "\n\nCaptured output:\n" + logs
        return ToolResult(
            text=text,
            is_error=True,
            meta={
                "kind": failure.kind,
                "sandbox": dict(sandbox),
                "logs": logs,
            },
        )


__all__ = ["NESTED_EXCLUDED_TOOLS", "RUN_CODE_NAME", "RunCodeTool"]
