"""Replay hand-authored native tool-call samples through the builtin tools."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx

from deeppyro.tools import builtin_tools
from deeppyro.tools.builtin.web import DUCKDUCKGO_HTML_ENDPOINT, FETCH_NOTICE
from tests.tools.helpers import make_context, write_file

CALLS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "native" / "tool_calls.json"
CALLS: dict[str, dict[str, Any]] = json.loads(CALLS_PATH.read_text(encoding="utf-8"))["calls"]
TOOLS = {tool.name: tool for tool in builtin_tools(object(), Path.cwd())}


def _args(name: str) -> dict[str, Any]:
    """Return a private copy of one hand-authored sample."""
    return json.loads(json.dumps(CALLS[name]))


def test_samples_reference_registered_tools() -> None:
    for name, args in CALLS.items():
        tool = TOOLS[name]
        declared = set(tool.parameters["properties"])
        assert set(args) <= declared, f"{name} sample used undeclared keys: {set(args) - declared}"
        required = set(tool.parameters.get("required", []))
        assert required <= set(args), f"{name} sample misses required fields {required - set(args)}"


async def test_read_replays_sample_arguments(tmp_path: Path) -> None:
    args = _args("read")
    target = write_file(tmp_path, "sample.txt", "first line\nsecond line\n")
    args["file_path"] = str(target)
    args.pop("offset", None)
    args.pop("limit", None)
    result = await TOOLS["read"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert "1: first line" in result.text
    assert "<type>file</type>" in result.text


async def test_write_replays_sample_arguments(tmp_path: Path) -> None:
    args = _args("write")
    target = tmp_path / "replayed_script.ps1"
    args["file_path"] = str(target)
    result = await TOOLS["write"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert "Created file" in result.text
    assert target.read_text(encoding="utf-8") == args["content"]
    assert result.meta["bytes"] == len(args["content"].encode("utf-8"))


async def test_edit_replays_sample_strings(tmp_path: Path) -> None:
    args = _args("edit")
    target = write_file(tmp_path, "notes.md", args["old_string"] + "\n")
    args["file_path"] = str(target)
    args.pop("replace_all", None)
    result = await TOOLS["edit"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert result.text == f"The file {target} has been updated successfully."
    assert args["new_string"] in target.read_text(encoding="utf-8")


async def test_grep_replays_sample_pattern(tmp_path: Path) -> None:
    args = _args("grep")
    tree = tmp_path / "wheel"
    write_file(tree, "gpu.py", "n_gpu_layers = 32\nCUDA = True\n")
    write_file(tree, "notes.txt", "CUDA mentioned outside python\n")
    args["path"] = str(tree)
    result = await TOOLS["grep"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert "gpu.py" in result.text
    assert "notes.txt" not in result.text
    assert result.meta["matches"] == 2


async def test_glob_replays_sample_pattern(tmp_path: Path) -> None:
    args = _args("glob")
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
    assert expected
    assert result.meta["count"] == len(expected)
    assert all(path.name in result.text for path in expected)


async def test_present_replays_sample_arguments(tmp_path: Path) -> None:
    args = _args("present")
    target = write_file(tmp_path, "deliverable.md", "# deliverable\n")
    args["files"][0]["path"] = str(target)
    result = await TOOLS["present"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert result.text == f"Presented {target}"


async def test_todo_replays_sample_arguments(tmp_path: Path) -> None:
    args = _args("todo_write")
    result = await TOOLS["todo_write"].run(args, make_context(tmp_path))
    assert result.is_error is False
    assert result.text == "Updated todo list: 1 pending, 1 in progress, 1 completed."


async def test_web_fetch_replays_sample_url(tmp_path: Path) -> None:
    url = CALLS["web_fetch"]["url"]
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


async def test_web_search_replays_sample_queries(tmp_path: Path) -> None:
    queries = CALLS["web_search"]["queries"]
    page = '<a class="result__a" href="https://learn.microsoft.com/bug-check">Bug check 0x10E</a>'
    with respx.mock() as mock:
        route = mock.post(DUCKDUCKGO_HTML_ENDPOINT).mock(return_value=httpx.Response(200, text=page))
        result = await TOOLS["web_search"].run({"queries": queries}, make_context(tmp_path))
    assert route.call_count == len(queries)
    assert result.is_error is False
    assert "- [Bug check 0x10E](https://learn.microsoft.com/bug-check)" in result.text
