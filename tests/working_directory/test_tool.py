"""Tests for the working_directory tool and plugin."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.errors import AbortedError, ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.tools.builtin.working_directory import WorkingDirectoryTool
from deeppyro.working_directory import WorkingDirectoryService

SESSION_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "sessions"


class FakeSession:
    def __init__(self, session_id: str, cwd: Path | None = None) -> None:
        self.session_id = session_id
        self.header = None if cwd is None else SimpleNamespace(cwd=str(cwd))


def make_context(base: Path, services: dict[str, Any] | None = None) -> ToolContext:
    return ToolContext(
        cwd=base,
        session_id="session-tool",
        turn=0,
        step=0,
        call_id="call_wd",
        cancel=asyncio.Event(),
        log=lambda kind, data: None,
        services={} if services is None else services,
    )


def make_service(
    base: Path, records: list[tuple[str, dict[str, Any]]] | None = None
) -> WorkingDirectoryService:
    service = WorkingDirectoryService({"defaultDirectory": str(base)})
    sink = records if records is not None else []
    service.configure(record=lambda kind, data: sink.append((kind, data)))
    return service


def load_fixture_calls() -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if not SESSION_DIR.is_dir():
        return calls
    for path in sorted(SESSION_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "tool/call":
                continue
            data = record.get("data") or {}
            if data.get("name") == "working_directory":
                calls.append(data)
    return calls


def test_schema_only_has_optional_cd() -> None:
    tool = WorkingDirectoryTool()
    assert tool.name == "working_directory"
    assert tool.parallel_safe is False
    assert tool.parameters == {
        "type": "object",
        "properties": {
            "cd": {
                "type": "string",
                "description": "Existing directory to enter. Omit to read the current directory.",
            }
        },
        "additionalProperties": False,
    }
    assert "required" not in tool.parameters


async def test_no_cd_reads_without_changing(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    records: list[tuple[str, dict[str, Any]]] = []
    service = make_service(base, records)
    result = await WorkingDirectoryTool().run({}, make_context(base, {"working_directory": service}))
    assert result.is_error is False
    assert result.text == str(base.resolve())
    assert result.meta == {"cwd": str(base.resolve())}
    assert records == []


async def test_cd_changes_and_returns_canonical_path(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    target = base / "src"
    target.mkdir()
    records: list[tuple[str, dict[str, Any]]] = []
    service = make_service(base, records)
    result = await WorkingDirectoryTool().run(
        {"cd": "src"}, make_context(base, {"working_directory": service})
    )
    assert result.is_error is False
    assert result.text == str(target.resolve())
    assert result.meta == {"cwd": str(target.resolve())}
    assert records == [("working-directory/change", {"cwd": str(target.resolve())})]
    assert service.get("session-tool") == target.resolve()


async def test_missing_service_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="working_directory service is not available"):
        await WorkingDirectoryTool().run({}, make_context(tmp_path, {}))


async def test_cd_must_be_a_string(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    with pytest.raises(ToolError, match="cd must be a string"):
        await WorkingDirectoryTool().run({"cd": None}, make_context(tmp_path, {"working_directory": service}))
    with pytest.raises(ToolError, match="cd must be a string"):
        await WorkingDirectoryTool().run({"cd": 3}, make_context(tmp_path, {"working_directory": service}))


async def test_cd_errors_propagate(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    ctx = make_context(tmp_path, {"working_directory": service})
    with pytest.raises(ToolError, match="cd must not be empty"):
        await WorkingDirectoryTool().run({"cd": "  "}, ctx)
    with pytest.raises(ToolError, match="directory does not exist"):
        await WorkingDirectoryTool().run({"cd": str(tmp_path / "missing")}, ctx)


async def test_cd_respects_cancel_event(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    ctx = make_context(tmp_path, {"working_directory": service})
    ctx.cancel.set()
    with pytest.raises(AbortedError):
        await WorkingDirectoryTool().run({"cd": str(target)}, ctx)


def test_fixture_arguments_are_accepted() -> None:
    calls = load_fixture_calls()
    if not calls:
        pytest.skip("working_directory fixture calls are unavailable")
    assert len(calls) >= 1
    properties = WorkingDirectoryTool.parameters["properties"]
    for data in calls:
        arguments = json.loads(data["arguments"])
        assert isinstance(arguments, dict)
        assert set(arguments) <= set(properties)
        assert isinstance(arguments.get("cd"), str)
        assert arguments["cd"].strip()


async def test_fixture_result_shape_with_equivalent_directory(tmp_path: Path) -> None:
    calls = load_fixture_calls()
    if not calls:
        pytest.skip("working_directory fixture calls are unavailable")
    target = tmp_path / "replay"
    target.mkdir()
    service = make_service(tmp_path)
    result = await WorkingDirectoryTool().run(
        {"cd": str(target)}, make_context(tmp_path, {"working_directory": service})
    )
    assert result.is_error is False
    assert Path(result.text) == target.resolve()
    assert result.meta["cwd"] == str(target.resolve())
    assert "\n" not in result.text


def test_plugin_provides_service_and_registers_tool(tmp_path: Path) -> None:
    from deeppyro.builtin.working_directory import WorkingDirectoryPlugin

    class FakeCtx:
        def __init__(self) -> None:
            self.provided: dict[str, Any] = {}
            self.factories: list[Any] = []

        def provide(self, name: str, value: Any) -> None:
            self.provided[name] = value

        def register_tool(self, factory: Any) -> None:
            self.factories.append(factory)

    ctx = FakeCtx()
    plugin = WorkingDirectoryPlugin(ctx, {"defaultDirectory": str(tmp_path)})
    assert plugin.provide == "working_directory"
    plugin.setup(ctx, plugin.config)
    assert ctx.provided["working_directory"] is plugin.service
    assert len(ctx.factories) == 1
    tools = ctx.factories[0](None, tmp_path)
    assert [tool.name for tool in tools] == ["working_directory"]
