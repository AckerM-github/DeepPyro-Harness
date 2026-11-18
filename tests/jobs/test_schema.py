"""The job tools' model-facing schemas must match the native schema fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deeppyro.tools.builtin.jobs import JobKillTool, JobListTool, JobOutputTool

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "native" / "tool_schemas.json"

TOOLS = {
    "job_output": JobOutputTool,
    "job_kill": JobKillTool,
    "job_list": JobListTool,
}


def _fixture_tools() -> dict[str, dict[str, Any]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload["tools"]


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_schema_matches_native_fixture(name: str) -> None:
    fixture = _fixture_tools()[name]
    tool = TOOLS[name]()
    ours = tool.parameters
    theirs = fixture["parameters"]
    assert tool.name == name
    assert tool.description == fixture["description"]
    assert ours["type"] == theirs["type"] == "object"
    assert list(ours["properties"]) == list(theirs["properties"])
    assert ours.get("required", []) == theirs.get("required", [])
    for key, schema in theirs["properties"].items():
        assert ours["properties"][key]["type"] == schema["type"]
