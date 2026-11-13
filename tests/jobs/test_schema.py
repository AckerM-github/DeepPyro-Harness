"""The job tools' model-facing schemas must match the DSH fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deeppyro.tools.builtin.jobs import JobKillTool, JobListTool, JobOutputTool

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
HEADER = FIXTURES / "records" / "by-type" / "request_header.jsonl"

TOOLS = {
    "job_output": JobOutputTool,
    "job_kill": JobKillTool,
    "job_list": JobListTool,
}


def _fixture_tools() -> dict[str, dict[str, Any]]:
    line = HEADER.read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    header = record["data"]["header"]
    return {tool["name"]: tool for tool in header["tools"]}


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_schema_matches_fixture(name: str) -> None:
    fixture = _fixture_tools().get(name)
    if fixture is None:
        pytest.skip(f"fixture does not contain {name}")
    tool = TOOLS[name]()
    ours = tool.parameters
    theirs = fixture["parameters"]
    assert tool.description == fixture["description"]
    assert ours["type"] == theirs["type"] == "object"
    assert list(ours["properties"]) == list(theirs["properties"])
    assert ours.get("required", []) == theirs.get("required", [])
    for key, schema in theirs["properties"].items():
        assert ours["properties"][key]["type"] == schema["type"]
