"""Tests for the model-facing run_code tool and its nested dispatch records."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.ptc import PtcRuntime, RunCodeTool
from deeppyro.sandbox import PermissionGate
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.registry import ToolRegistry

from .helpers import make_context


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo a value."
    parameters: ClassVar[dict[str, Any]] = make_parameters({"value": {"type": "string"}}, required=["value"])
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(text="echo:" + str(args["value"]), meta={"value": args["value"]})


class BoomTool(BaseTool):
    name = "boom"
    description = "Always fail."
    parameters: ClassVar[dict[str, Any]] = make_parameters({})
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise ToolError("boom")


class DeniedTool(BaseTool):
    name = "denied"
    description = "Permission denied."
    parameters: ClassVar[dict[str, Any]] = make_parameters({})
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise PermissionDenied("no way")


class ContextTool(BaseTool):
    name = "inspect"
    description = "Inspect the nested context."
    parameters: ClassVar[dict[str, Any]] = make_parameters({})
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            text="ok",
            meta={
                "cwd": str(ctx.cwd),
                "session": ctx.session_id,
                "turn": ctx.turn,
                "step": ctx.step,
                "callId": ctx.call_id,
                "hasRegistry": "tool_registry" in ctx.services,
            },
        )


class ImageTool(BaseTool):
    name = "image"
    description = "Return an image part."
    parameters: ClassVar[dict[str, Any]] = make_parameters({})
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            text="attached",
            parts=[{"type": "image", "attachmentId": "att-1", "mediaType": "image/png"}],
        )


def tool_context(
    tmp_path: Path,
    services: dict[str, Any],
    *,
    permission: Any | None = None,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> ToolContext:
    return make_context(tmp_path, services=services, permission=permission, events=events)


def test_schema_and_flags() -> None:
    tool = RunCodeTool()
    assert tool.name == "run_code"
    assert tool.parallel_safe is False
    assert tool.reserved_tool is True
    assert tool.parameters["type"] == "object"
    assert set(tool.parameters["properties"]) == {"code", "description"}
    assert tool.parameters["required"] == ["code", "description"]
    assert tool.parameters["additionalProperties"] is False
    assert "fresh CPython process" in tool.description
    assert "Each program starts in the Session's current directory." in tool.description


def test_schema_includes_timeout_with_runtime() -> None:
    tool = RunCodeTool(PtcRuntime())
    assert set(tool.parameters["properties"]) == {"code", "description", "timeoutMs"}
    assert tool.parameters["properties"]["timeoutMs"]["maximum"] == 600_000
    assert "Execute a Python program against the available tools" in tool.description


async def test_missing_service_raises(tmp_path: Path) -> None:
    tool = RunCodeTool()
    with pytest.raises(ToolError):
        await tool.run({"code": "return 1", "description": "one"}, tool_context(tmp_path, {}))


async def test_empty_description_raises(tmp_path: Path) -> None:
    tool = RunCodeTool()
    with pytest.raises(ToolError, match="invalid description"):
        await tool.run({"code": "return 1", "description": "   "}, tool_context(tmp_path, {}))


async def test_read_only_is_rejected(tmp_path: Path) -> None:
    tool = RunCodeTool()
    gate = PermissionGate("read-only", "never", tmp_path)
    result = await tool.run(
        {"code": "return 1", "description": "one"},
        tool_context(tmp_path, {"ptc": PtcRuntime()}, permission=gate),
    )
    assert result.is_error is True
    assert "read-only" in result.text


async def test_success_value_and_meta(tmp_path: Path) -> None:
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "return {'answer': 42}", "description": "answer"},
        tool_context(tmp_path, {"ptc": PtcRuntime()}),
    )
    assert result.is_error is False
    assert result.text == '{\n  "answer": 42\n}'
    assert result.meta is not None
    assert result.meta["result"] == {"answer": 42}
    assert result.meta["sandbox"]["enforcement"] == "none"


async def test_empty_output_placeholder(tmp_path: Path) -> None:
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "return None", "description": "nothing"},
        tool_context(tmp_path, {"ptc": PtcRuntime()}),
    )
    assert result.text == "(run_code completed with no output)"


async def test_logs_only_output(tmp_path: Path) -> None:
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "print('hello')\nreturn None", "description": "print"},
        tool_context(tmp_path, {"ptc": PtcRuntime()}),
    )
    assert result.text == "hello"


async def test_failure_text(tmp_path: Path) -> None:
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "return 1 / 0", "description": "divide"},
        tool_context(tmp_path, {"ptc": PtcRuntime()}),
    )
    assert result.is_error is True
    assert result.text.startswith("Error: code run failed (exception): ZeroDivisionError")
    assert result.meta is not None
    assert result.meta["kind"] == "exception"


async def test_failure_includes_captured_output(tmp_path: Path) -> None:
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "print('before')\nreturn 1 / 0", "description": "divide"},
        tool_context(tmp_path, {"ptc": PtcRuntime()}),
    )
    assert "Captured output:" in result.text
    assert "before" in result.text


async def test_invalid_timeout_raises(tmp_path: Path) -> None:
    tool = RunCodeTool()
    with pytest.raises(ToolError):
        await tool.run(
            {"code": "return 1", "description": "one", "timeoutMs": 0},
            tool_context(tmp_path, {"ptc": PtcRuntime()}),
        )


async def test_dispatch_success_and_events(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    registry = ToolRegistry([EchoTool()])
    tool = RunCodeTool()
    result = await tool.run(
        {
            "code": "value = await tools.echo({'value': 'hi'})\nreturn value",
            "description": "echo",
        },
        tool_context(tmp_path, {"ptc": PtcRuntime(), "tool_registry": registry}, events=events),
    )
    assert result.is_error is False
    assert result.text == "echo:hi"
    kinds = [kind for kind, _ in events]
    assert kinds == ["tool/ptc-dispatch-start", "tool/ptc-dispatch"]
    start = events[0][1]
    assert start["rootCallId"] == "call_ptc_test"
    assert start["parentCallId"] == "call_ptc_test"
    assert start["subCallId"] == "call_ptc_test:ptc:1"
    assert start["name"] == "echo"
    assert start["arguments"] == {"value": "hi"}
    dispatch = events[1][1]
    assert dispatch["isError"] is False
    assert dispatch["content"] == [{"type": "text", "text": "echo:hi"}]
    assert dispatch["meta"] == {"value": "hi"}
    assert dispatch["subCallId"] == start["subCallId"]


async def test_dispatch_tool_error_is_catchable(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    registry = ToolRegistry([BoomTool()])
    tool = RunCodeTool()
    program = "try:\n    await tools.boom({})\nexcept ToolCallError as exc:\n    return exc.toolName\n"
    result = await tool.run(
        {"code": program, "description": "boom"},
        tool_context(tmp_path, {"ptc": PtcRuntime(), "tool_registry": registry}, events=events),
    )
    assert result.is_error is False
    assert result.text == "boom"
    dispatch = events[1][1]
    assert dispatch["isError"] is True
    assert dispatch["error"]["code"] == "TOOL_ERROR"
    assert dispatch["error"]["reason"] == "boom"
    assert dispatch["content"] == [{"type": "text", "text": "Error: boom"}]


async def test_dispatch_permission_denied_is_structured(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    registry = ToolRegistry([DeniedTool()])
    tool = RunCodeTool()
    program = "try:\n    await tools.denied({})\nexcept ToolCallError:\n    return 'caught'\n"
    result = await tool.run(
        {"code": program, "description": "denied"},
        tool_context(tmp_path, {"ptc": PtcRuntime(), "tool_registry": registry}, events=events),
    )
    assert result.is_error is False
    dispatch = events[1][1]
    assert dispatch["error"]["code"] == "PERMISSION_DENIED"


async def test_unknown_tool_is_catchable(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    tool = RunCodeTool()
    program = "try:\n    await tools.nope({})\nexcept ToolCallError as exc:\n    return str(exc)\n"
    result = await tool.run(
        {"code": program, "description": "unknown"},
        tool_context(tmp_path, {"ptc": PtcRuntime(), "tool_registry": ToolRegistry()}, events=events),
    )
    assert result.is_error is False
    assert "unknown tool" in result.text
    assert events == []


async def test_dispatch_inherits_context(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    registry = ToolRegistry([ContextTool()])
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "return await tools.inspect({})", "description": "inspect"},
        tool_context(tmp_path, {"ptc": PtcRuntime(), "tool_registry": registry}, events=events),
    )
    assert result.is_error is False
    dispatch = events[1][1]
    meta = dispatch["meta"]
    assert meta["cwd"] == str(tmp_path)
    assert meta["session"] == "session-ptc"
    assert meta["turn"] == 2
    assert meta["step"] == 3
    assert meta["callId"] == "call_ptc_test:ptc:1"
    assert meta["hasRegistry"] is True


async def test_nested_excluded_tools_are_not_bound(tmp_path: Path) -> None:
    class AskTool(EchoTool):
        name = "ask_user_question"

    registry = ToolRegistry([AskTool()])
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "await tools.ask_user_question({})", "description": "ask"},
        tool_context(tmp_path, {"ptc": PtcRuntime(), "tool_registry": registry}),
    )
    assert result.is_error is True
    assert "ask_user_question" in result.text


async def test_successful_image_parts_are_deferred(tmp_path: Path) -> None:
    registry = ToolRegistry([ImageTool()])
    tool = RunCodeTool()
    result = await tool.run(
        {"code": "return await tools.image({})", "description": "image"},
        tool_context(tmp_path, {"ptc": PtcRuntime(), "tool_registry": registry}),
    )
    assert result.is_error is False
    assert result.text == "attached"
    assert result.parts == [{"type": "image", "attachmentId": "att-1", "mediaType": "image/png"}]
