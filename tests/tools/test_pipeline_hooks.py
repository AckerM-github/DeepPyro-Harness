"""Tool pipeline hooks: pre/execute/post decisions, call seq, concurrency."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools import ToolCallRequest, ToolRegistry, run_tool_batch
from deeppyro.tools.base import BaseTool
from tests.tools.helpers import make_context

Handler = Any


class RecordingTool(BaseTool):
    """A tool that records calls and can report concurrent execution."""

    def __init__(
        self,
        name: str = "echo",
        *,
        result: ToolResult | None = None,
        delay: float = 0.0,
        parallel_safe: bool = True,
        state: dict[str, int] | None = None,
    ) -> None:
        self.name = name
        self.description = name
        self.parameters = {"type": "object", "properties": {}}
        self.parallel_safe = parallel_safe
        self.result = result
        self.delay = delay
        self.state = state
        self.calls: list[dict[str, Any]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        if self.state is not None:
            self.state["current"] += 1
            self.state["max"] = max(self.state["max"], self.state["current"])
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self.result or ToolResult(text=f"{self.name}:{args}")
        finally:
            if self.state is not None:
                self.state["current"] -= 1


class Hooks:
    """Duck-typed hooks object using the async waterfall contract."""

    def __init__(
        self,
        *,
        pre: Handler | None = None,
        execute: Handler | None = None,
        post: Handler | None = None,
    ) -> None:
        self.handlers: dict[str, Handler | None] = {
            "tools/pre-execute": pre,
            "tools/execute": execute,
            "tools/post-execute": post,
        }
        self.events: list[str] = []
        self.payloads: dict[str, dict[str, Any]] = {}

    async def waterfall(self, name: str, payload: dict[str, Any], fallback: Handler) -> Any:
        self.events.append(name)
        self.payloads[name] = payload
        handler = self.handlers.get(name)
        if handler is None:
            return await fallback()
        return await handler(payload, fallback)


def request(call_id: str, name: str, arguments: str = "{}") -> ToolCallRequest:
    return ToolCallRequest(call_id=call_id, name=name, arguments=arguments)


async def run(
    registry: ToolRegistry,
    requests: list[ToolCallRequest],
    *,
    hooks: Hooks | None = None,
    max_concurrency: int = 8,
    turn: int | None = 3,
    step: int | None = 2,
    log: Any | None = None,
    dispatch: Any | None = None,
    services: dict[str, Any] | None = None,
) -> list[Any]:
    return await run_tool_batch(
        requests,
        registry=registry,
        ctx_factory=lambda req: make_context(Path.cwd(), services=services, call_id=req.call_id),
        max_concurrency=max_concurrency,
        hooks=hooks,
        turn=turn,
        step=step,
        log=log,
        dispatch=dispatch,
    )


async def test_hooks_absent_keeps_legacy_result_and_call_seq_default() -> None:
    tool = RecordingTool()
    outcomes = await run(ToolRegistry([tool]), [request("c1", "echo")])
    assert outcomes[0].result.text == "echo:{}"
    assert outcomes[0].result.is_error is False
    assert outcomes[0].call_seq is None
    assert tool.calls == [{}]


async def test_pre_allow_runs_all_three_events_with_wire_payloads() -> None:
    hooks = Hooks()
    tool = RecordingTool()
    outcomes = await run(
        ToolRegistry([tool]),
        [request("c1", "echo", '{"value": 1}')],
        hooks=hooks,
    )
    assert outcomes[0].result.text == "echo:{'value': 1}"
    assert hooks.events == [
        "tools/pre-execute",
        "tools/execute",
        "tools/post-execute",
    ]
    pre = hooks.payloads["tools/pre-execute"]
    assert pre["call"] == {"callId": "c1", "name": "echo", "arguments": '{"value": 1}'}
    assert pre["tool"] == "echo"
    assert pre["turn"] == 3
    assert pre["step"] == 2
    execute = hooks.payloads["tools/execute"]
    assert execute["arguments"] == {"value": 1}
    post = hooks.payloads["tools/post-execute"]
    assert post["result"].text == "echo:{'value': 1}"


async def test_pre_deny_short_circuits_and_never_runs_the_tool() -> None:
    async def deny(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "deny", "reason": "policy says no"}

    hooks = Hooks(pre=deny)
    tool = RecordingTool()
    outcomes = await run(ToolRegistry([tool]), [request("c1", "echo")], hooks=hooks)
    assert outcomes[0].result.is_error is True
    assert outcomes[0].result.text == "Error: policy says no"
    assert tool.calls == []
    assert hooks.events == ["tools/pre-execute"]


async def test_pre_deny_without_reason_uses_default_text() -> None:
    async def deny(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "deny"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(pre=deny))
    assert outcomes[0].result.text == "Error: tool execution denied"


async def test_pre_ask_degrades_to_denial_with_clear_reason() -> None:
    async def ask(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "ask", "reason": "needs network access"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(pre=ask))
    assert outcomes[0].result.is_error is True
    assert "requires approval" in outcomes[0].result.text
    assert "no approval mechanism is available" in outcomes[0].result.text
    assert "needs network access" in outcomes[0].result.text


async def test_pre_cancel_degrades_to_denial() -> None:
    async def cancel(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "cancel", "reason": "user stopped the turn"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(pre=cancel))
    assert outcomes[0].result.is_error is True
    assert "cancelled" in outcomes[0].result.text
    assert "user stopped the turn" in outcomes[0].result.text


async def test_execute_hook_can_replace_the_result() -> None:
    async def override(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "result", "result": ToolResult(text="hooked", meta={"by": "hook"})}

    hooks = Hooks(execute=override)
    tool = RecordingTool()
    outcomes = await run(ToolRegistry([tool]), [request("c1", "echo")], hooks=hooks)
    assert outcomes[0].result.text == "hooked"
    assert outcomes[0].result.meta == {"by": "hook"}
    assert tool.calls == []
    assert hooks.events == ["tools/pre-execute", "tools/execute", "tools/post-execute"]


async def test_execute_hook_can_return_a_toolresult_directly() -> None:
    async def direct(payload: dict[str, Any], fallback: Any) -> ToolResult:
        return ToolResult(text="direct", is_error=False)

    outcomes = await run(
        ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(execute=direct)
    )
    assert outcomes[0].result.text == "direct"


async def test_dispatch_callback_replaces_tool_run_even_without_hooks() -> None:
    seen: list[tuple[str, dict[str, Any], ToolContext]] = []

    async def dispatch(tool: Any, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        seen.append((tool.name, args, ctx))
        return ToolResult(text="dispatched")

    tool = RecordingTool()
    outcomes = await run(ToolRegistry([tool]), [request("c1", "echo", '{"x": 1}')], dispatch=dispatch)
    assert outcomes[0].result.text == "dispatched"
    assert tool.calls == []
    assert seen[0][0] == "echo"
    assert seen[0][1] == {"x": 1}
    assert seen[0][2].call_id == "c1"


async def test_post_accept_content_replaces_the_result_text() -> None:
    async def content(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "accept", "content": "clean text"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(post=content))
    assert outcomes[0].result.text == "clean text"
    assert outcomes[0].result.is_error is False


async def test_post_accept_value_is_carried_in_meta() -> None:
    async def value(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "accept", "value": {"answer": 42}}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(post=value))
    assert outcomes[0].result.is_error is False
    assert outcomes[0].result.meta is not None
    assert outcomes[0].result.meta["value"] == {"answer": 42}


async def test_post_accept_value_cannot_replace_a_failed_result() -> None:
    async def value(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "accept", "value": "nope"}

    tool = RecordingTool(result=ToolResult(text="tool failed", is_error=True))
    outcomes = await run(ToolRegistry([tool]), [request("c1", "echo")], hooks=Hooks(post=value))
    assert outcomes[0].result.is_error is True
    assert "cannot replace the value of a failed result" in outcomes[0].result.text


async def test_post_accept_rejects_content_and_value_together() -> None:
    async def both(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "accept", "content": "x", "value": "y"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(post=both))
    assert outcomes[0].result.is_error is True
    assert "cannot replace both value and content" in outcomes[0].result.text


async def test_post_block_replaces_the_result_with_feedback() -> None:
    async def block(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "block", "feedback": "use a narrower pattern"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(post=block))
    assert outcomes[0].result.is_error is True
    assert outcomes[0].result.text == "use a narrower pattern"


async def test_post_block_without_feedback_uses_default_text() -> None:
    async def block(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "block"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(post=block))
    assert outcomes[0].result.is_error is True
    assert outcomes[0].result.text == "tool result blocked by post-execute policy"


async def test_call_seq_is_recorded_and_log_runs_before_pre_execute() -> None:
    events: list[str] = []
    logged: list[tuple[int | None, int | None, dict[str, Any]]] = []

    def log(turn: int | None, step: int | None, call: dict[str, Any]) -> int:
        logged.append((turn, step, call))
        events.append(f"log:{call['callId']}")
        return 10 + len(logged)

    async def pre(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        events.append("pre")
        return await fallback()

    hooks = Hooks(pre=pre)
    outcomes = await run(
        ToolRegistry([RecordingTool()]),
        [request("c1", "echo"), request("c2", "echo")],
        hooks=hooks,
        log=log,
        turn=4,
        step=5,
    )
    assert [outcome.call_seq for outcome in outcomes] == [11, 12]
    assert logged[0] == (4, 5, {"callId": "c1", "name": "echo", "arguments": "{}"})
    assert events[:2] == ["log:c1", "log:c2"]
    assert events[2:] == ["pre", "pre"] or events[2:].count("pre") == 2


async def test_log_failure_becomes_an_error_result_without_running_the_tool() -> None:
    def log(turn: int | None, step: int | None, call: dict[str, Any]) -> int:
        raise RuntimeError("session store is closed")

    tool = RecordingTool()
    outcomes = await run(ToolRegistry([tool]), [request("c1", "echo")], log=log)
    assert outcomes[0].result.is_error is True
    assert "cannot record tool call" in outcomes[0].result.text
    assert "session store is closed" in outcomes[0].result.text
    assert outcomes[0].call_seq is None
    assert tool.calls == []


async def test_hook_exceptions_become_error_results() -> None:
    async def pre(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        raise ToolError("hook exploded")

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(pre=pre))
    assert outcomes[0].result.is_error is True
    assert outcomes[0].result.text == "Error: hook exploded"


async def test_invalid_pre_decision_kind_is_an_error_result() -> None:
    async def pre(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "maybe"}

    outcomes = await run(ToolRegistry([RecordingTool()]), [request("c1", "echo")], hooks=Hooks(pre=pre))
    assert outcomes[0].result.is_error is True
    assert "invalid tools/pre-execute decision kind" in outcomes[0].result.text


async def test_execute_result_that_is_not_a_toolresult_is_an_error() -> None:
    async def execute(payload: dict[str, Any], fallback: Any) -> dict[str, Any]:
        return {"kind": "result", "result": "not a ToolResult"}

    outcomes = await run(
        ToolRegistry([RecordingTool()]),
        [request("c1", "echo")],
        hooks=Hooks(execute=execute),
    )
    assert outcomes[0].result.is_error is True
    assert "not ToolResult" in outcomes[0].result.text


async def test_concurrency_and_request_order_survive_hooks() -> None:
    state = {"current": 0, "max": 0}
    tool = RecordingTool(delay=0.03, state=state)
    hooks = Hooks()
    requests = [request(f"c{index}", "echo") for index in range(6)]
    outcomes = await run(ToolRegistry([tool]), requests, hooks=hooks, max_concurrency=3)
    assert [outcome.call_id for outcome in outcomes] == [f"c{index}" for index in range(6)]
    assert state["max"] == 3
    assert hooks.events.count("tools/execute") == 6
