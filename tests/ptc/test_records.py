"""Tests that nested PTC dispatch records stay out of the model surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.core.wire import RECORD_TYPES
from deeppyro.ptc import PtcRuntime, RunCodeTool
from deeppyro.session.events import SURFACE_EVENTS
from deeppyro.session.store import SessionStore
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.registry import ToolRegistry

from .helpers import make_context


class EchoTool(BaseTool):
    name = "echo"
    description = "Echo a value."
    parameters: ClassVar[dict[str, Any]] = make_parameters({"value": {"type": "string"}}, required=["value"])
    parallel_safe = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(text="echo:" + str(args["value"]))


def test_dispatch_record_types_are_log_only() -> None:
    for record_type in ("tool/ptc-dispatch-start", "tool/ptc-dispatch"):
        assert record_type in RECORD_TYPES
        assert record_type not in SURFACE_EVENTS


async def test_dispatch_records_do_not_appear_in_derived_messages(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    session = store.create(tmp_path)
    ctx = make_context(
        tmp_path,
        services={"ptc": PtcRuntime(), "tool_registry": ToolRegistry([EchoTool()])},
    )
    ctx.log = lambda kind, data: session.append(kind, data)
    result = await RunCodeTool().run({"program": "return await echo(value='x')"}, ctx)
    assert result.is_error is False
    assert [record.type for record in session.records] == ["tool/ptc-dispatch-start", "tool/ptc-dispatch"]
    assert session.derive_messages() == []
