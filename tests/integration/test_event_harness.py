"""Runtime harvest of the bus events deeppyro actually dispatches.

The static catalog records what the shipped DSH app dispatches; this harness
records what deeppyro dispatches while a real agent loop, the tools pipeline and
the approval gate run against in-memory fakes. It is the evidence behind the
"static N / runtime X / difference" numbers: a name only counts when a dispatch
site fired for real, not when a constant exists.

`llm/stream` needs a live listener to fire because the agent takes a fast path
when nothing subscribes to a hook (`_hook_enabled`); the harness therefore
subscribes one no-op listener per emit-only hook it wants to observe.

Run ``uv run pytest tests/integration/test_event_harness.py -s`` to print the
harvested names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.core.events import names as bus_names
from deeppyro.core.protocols import TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.hooks import HookService
from deeppyro.hooks.types import HOOK_NAMES, LLM_STREAM
from deeppyro.sandbox.gate import ApprovalRequest, PermissionGate
from tests.agent.fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    FakeTool,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)

#: Dispatch sites this smoke is expected to reach. Anything missing means a
#: producer stopped firing, which the static catalog cannot tell you.
MINIMUM_COVERAGE = {
    "agent/pre-step",
    "agent/request",
    "agent/status",
    "agent/turn-stopping",
    "approval/request",
    "llm/stream",
    "system-prompt/assemble",
    "tools/execute",
    "tools/post-execute",
    "tools/pre-execute",
}


async def test_the_harness_sees_only_declared_hook_names(tmp_path: Path) -> None:
    """Every dispatched name is one deeppyro declares, and the core paths fire."""
    hooks = HookService()
    seen: set[str] = set()
    hooks.add_observer(lambda observation: seen.add(observation.hook))
    hooks.on(LLM_STREAM, lambda payload: None)

    tool = FakeTool("echo", text="ok")
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(
        [
            ScriptedTurn(
                events=[
                    ToolCallStart("call_1", "echo"),
                    ToolCallArgsDelta("call_1", "{}"),
                    finish("tool-calls"),
                ]
            ),
            ScriptedTurn(events=[TextDelta("done"), finish("stop")]),
        ]
    )
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry([tool]),
        options=AgentOptions(model="test-model"),
        sink=RecordingSink(session),
        permission=FakePermission(),
        cwd=tmp_path,
        services={"hooks": hooks},
    )

    assert await agent.send("go") == "done"

    gate = PermissionGate(
        "workspace-write",
        "ask",
        tmp_path,
        ask=lambda request: True,
        hooks=hooks,
        session=session,
    )
    await gate.request(ApprovalRequest(kind="write", summary="write a.txt"))

    observed: Any = sorted(seen)
    print("runtime-observed:", observed)

    assert seen, "the harness must observe at least one dispatch"
    declared = set(bus_names(include_reserved=False))
    assert seen <= declared, sorted(seen - declared)
    assert set(HOOK_NAMES) <= declared, sorted(set(HOOK_NAMES) - declared)
    assert MINIMUM_COVERAGE <= seen, sorted(MINIMUM_COVERAGE - seen)
