"""Regression samples reconstructed from fixtures/records and fixtures/sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from deeppyro.tools import builtin_tools
from deeppyro.tools.builtin.web import DUCKDUCKGO_HTML_ENDPOINT, FETCH_NOTICE
from tests.tools.helpers import FIXTURES, load_real_tool_calls, make_context, write_file

if not FIXTURES.is_dir():
    pytest.skip(
        "real DSH fixtures not present; run: node tools/gen_fixtures.mjs",
        allow_module_level=True,
    )

TOOLS = {tool.name: tool for tool in builtin_tools(object(), Path.cwd())}


@pytest.fixture(scope="module")
def real_calls() -> dict[str, list[dict[str, Any]]]:
    calls = load_real_tool_calls()
    if not calls:
        pytest.skip("fixtures are not available in this checkout")
    return calls


def test_fixture_records_are_present() -> None:
    tool_call = FIXTURES / "records" / "by-type" / "tool_call.jsonl"
    assert tool_call.is_file()
    with tool_call.open(encoding="utf-8") as handle:
        record = json.loads(handle.readline())
    assert record["type"] == "tool/call"
    assert {"turn", "step", "callId", "name", "arguments"} <= set(record["data"])


def test_real_arguments_only_use_declared_properties(real_calls) -> None:  # type: ignore[no-untyped-def]
    for name, calls in real_calls.items():
        tool = TOOLS[name]
        declared = set(tool.parameters["properties"])
        required = set(tool.parameters.get("required", []))
        for args in calls:
            assert set(args) <= declared, f"{name} call used undeclared keys: {set(args) - declared}"
        assert any(required <= set(args) for args in calls), (
            f"no real {name} call satisfies the required fields"
        )


async def test_read_replays_real_arguments(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    args = dict(real_calls["read"][0])
    target = write_file(tmp_path, "sample.txt", "first line\nsecond line\n")
    args["file_path"] = str(target)
    args.pop("offset", None)
    args.pop("limit", None)
    result = await TOOLS["read"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert "1: first line" in result.text
    assert "<type>file</type>" in result.text


async def test_write_replays_real_arguments(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    args = dict(real_calls["write"][0])
    target = tmp_path / "replayed_script.ps1"
    args["file_path"] = str(target)
    result = await TOOLS["write"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert "Created file" in result.text
    assert target.read_text(encoding="utf-8") == args["content"]
    assert result.meta["bytes"] == len(args["content"].encode("utf-8"))


async def test_edit_replays_real_strings(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    args = dict(real_calls["edit"][0])
    target = write_file(tmp_path, "notes.md", args["old_string"] + "\n")
    args["file_path"] = str(target)
    args.pop("replace_all", None)
    result = await TOOLS["edit"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert result.text == f"The file {target} has been updated successfully."
    assert args["new_string"] in target.read_text(encoding="utf-8")


async def test_grep_replays_real_pattern(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    args = dict(real_calls["grep"][0])
    tree = tmp_path / "wheel"
    write_file(tree, "gpu.py", "n_gpu_layers = 32\nCUDA = True\n")
    write_file(tree, "notes.txt", "CUDA mentioned outside python\n")
    args["path"] = str(tree)
    result = await TOOLS["grep"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert "gpu.py" in result.text
    assert "notes.txt" not in result.text
    assert result.meta["matches"] == 2


async def test_glob_replays_real_pattern(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    args = dict(real_calls["glob"][0])
    tree = tmp_path / "tree"
    write_file(tree, "strata_a/nested/x.txt", "x")
    write_file(tree, "strata_b/y.txt", "y")
    write_file(tree, "other/z.txt", "z")
    args["path"] = str(tree)
    result = await TOOLS["glob"].run(args, make_context(tmp_path))
    assert result.is_error is False
    expected = [
        path
        for path in tree.glob(args["pattern"])
        if path.is_file() and not {".git", ".hg", ".svn", ".bzr", "CVS"} & set(path.parts)
    ]
    assert result.meta["count"] == len(expected)
    if expected:
        assert all(path.name in result.text for path in expected)


async def test_present_replays_real_arguments(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    args = json.loads(json.dumps(real_calls["present"][0]))
    target = write_file(tmp_path, "deliverable.md", "# deliverable\n")
    args["files"][0]["path"] = str(target)
    result = await TOOLS["present"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert result.text == f"Presented {target}"


async def test_todo_replays_real_arguments(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    args = json.loads(json.dumps(real_calls["todo_write"][0]))
    result = await TOOLS["todo_write"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert result.text == "Updated todo list: 1 pending, 0 in progress, 0 completed."


async def test_web_fetch_replays_real_url(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    url = real_calls["web_fetch"][0]["url"]
    with respx.mock() as mock:
        mock.get(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body><h1>Bug check</h1><p>Details</p></body></html>",
            )
        )
        result = await TOOLS["web_fetch"].run({"url": url}, make_context(tmp_path))
    assert result.is_error is False
    assert result.text.startswith(f"Fetched {url} (HTTP 200)")
    assert FETCH_NOTICE in result.text
    assert "Bug check" in result.text


async def test_web_search_replays_real_queries(real_calls, tmp_path) -> None:  # type: ignore[no-untyped-def]
    queries = real_calls["web_search"][0]["queries"]
    page = '<a class="result__a" href="https://learn.microsoft.com/bug-check">Bug check 0x10E</a>'
    with respx.mock() as mock:
        route = mock.post(DUCKDUCKGO_HTML_ENDPOINT).mock(return_value=httpx.Response(200, text=page))
        result = await TOOLS["web_search"].run({"queries": queries}, make_context(tmp_path))
    assert route.call_count == len(queries)
    assert result.is_error is False
    assert "- [Bug check 0x10E](https://learn.microsoft.com/bug-check)" in result.text
