"""Byte-for-byte equivalence snapshot of every model-facing tool schema.

The schema dicts are the contract with the model: they must stay stable across
refactors. This test renders every tool the runtime can register and compares
its `parameters` object (key order included) with a checked-in snapshot, so a
pydantic derivation bug shows up as a diff instead of a behaviour change.

Regenerate deliberately (and review the diff!) with::

    DEEPYRO_UPDATE_SCHEMAS=1 uv run --no-sync pytest tests/tools/test_tool_schemas.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.mcp.bridge import McpToolAdapter
from deeppyro.mcp.resources import resource_tools
from deeppyro.ptc.tool import RunCodeTool
from deeppyro.tools.builtin import builtin_tools
from deeppyro.tools.builtin.goal import CreateGoalTool, GetGoalTool, GoalTool, UpdateGoalTool
from deeppyro.tools.builtin.jobs import job_tools
from deeppyro.tools.builtin.memory import memory_tools
from deeppyro.tools.builtin.plugin_tools import plugin_tools
from deeppyro.tools.builtin.schedule import schedule_tools
from deeppyro.tools.builtin.skill import SkillTool
from deeppyro.tools.builtin.subagent import subagent_tools
from deeppyro.tools.builtin.team import team_tools

SNAPSHOT = Path(__file__).with_name("tool_schema_snapshot.json")
UPDATE_ENV = "DEEPYRO_UPDATE_SCHEMAS"


class _FakeMcpService:
    """Just enough of McpService for the three resource tools."""

    def server_names(self) -> list[str]:
        return ["demo"]

    def state(self, server: str) -> Any:
        return object() if server == "demo" else None


def _remote_tool(input_schema: Any) -> SimpleNamespace:
    return SimpleNamespace(
        name="echo",
        description="Echo one argument.",
        input_schema=input_schema,
        annotations=None,
    )


def _mcp_adapter(schema: Any) -> McpToolAdapter:
    return McpToolAdapter(SimpleNamespace(), _remote_tool(schema))


def _tools() -> dict[str, Any]:
    """Every static tool the runtime registers, keyed by its public name."""
    tools = [
        *builtin_tools(None, Path.cwd()),
        *job_tools(),
        *plugin_tools(),
        *memory_tools(),
        *schedule_tools(),
        *subagent_tools(),
        *team_tools(),
        GetGoalTool(),
        CreateGoalTool(),
        UpdateGoalTool(),
        GoalTool(),
        SkillTool(service=None),
        RunCodeTool(),
        *resource_tools(_FakeMcpService()),  # type: ignore[arg-type]
    ]
    return {tool.name: tool.parameters for tool in tools}


def _variants() -> dict[str, Any]:
    """Schemas that depend on runtime configuration rather than the tool class."""
    return {
        "run_code[timeout]": RunCodeTool(SimpleNamespace(timeout={"maxMs": 600_000})).parameters,
        "mcp_adapter[object]": _mcp_adapter(
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query."}},
                "required": ["query"],
            }
        ).parameters,
        "mcp_adapter[fallback]": _mcp_adapter({"type": "string"}).parameters,
    }


def _render(schemas: dict[str, Any]) -> dict[str, str]:
    # json.dumps keeps insertion order, so this also pins the key order of
    # every schema node (the model sees the exact bytes below).
    return {name: json.dumps(schema, ensure_ascii=False) for name, schema in schemas.items()}


def test_tool_schemas_match_the_snapshot() -> None:
    current = {"tools": _tools(), "variants": _variants()}
    if os.environ.get(UPDATE_ENV):
        SNAPSHOT.write_text(
            json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    expected = {**_render(snapshot["tools"]), **_render(snapshot["variants"])}
    rendered = {**_render(current["tools"]), **_render(current["variants"])}
    assert sorted(rendered) == sorted(expected), (
        "tool schema snapshot coverage changed: "
        f"missing={sorted(set(expected) - set(rendered))}, "
        f"new={sorted(set(rendered) - set(expected))}"
    )
    for name in sorted(expected):
        assert rendered[name] == expected[name], f"schema drift for {name}"


def test_snapshot_covers_the_whole_tool_set() -> None:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    names = set(snapshot["tools"])
    # 13 builtin + 3 jobs + 5 plugin + 1 memory + 1 schedule + 2 subagent
    # + 9 team + 4 goal + 1 skill + 1 ptc + 3 mcp resource.
    assert len(names) == 43, sorted(names)
    assert names >= {"read", "write", "edit", "pwsh", "bash", "run_code", "schedule"}
