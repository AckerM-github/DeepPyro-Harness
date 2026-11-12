"""ToolRegistry behaviour: ordering, lookup, duplicate names, schemas."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolResult, ToolSchema
from deeppyro.tools import ToolRegistry, builtin_tools
from deeppyro.tools.base import BaseTool


class _Echo(BaseTool):
    name = "echo"
    description = "echo"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def run(self, args, ctx):  # type: ignore[no-untyped-def]
        return ToolResult(text="ok")


def test_registry_preserves_registration_order() -> None:
    first = _Echo()
    second = _Echo()
    second.name = "echo2"  # type: ignore[misc]
    registry = ToolRegistry([first, second])
    assert registry.names() == ["echo", "echo2"]
    assert len(registry) == 2
    assert "echo" in registry
    assert "missing" not in registry
    assert list(registry) == [first, second]


def test_registry_get_and_duplicate_name() -> None:
    registry = ToolRegistry([_Echo()])
    assert registry.get("echo") is not None
    assert registry.get("nope") is None
    with pytest.raises(ToolError, match="already registered"):
        registry.register(_Echo())


def test_registry_schemas_are_model_facing() -> None:
    registry = ToolRegistry([_Echo()])
    schemas = registry.schemas()
    assert schemas == [
        ToolSchema(name="echo", description="echo", parameters={"type": "object", "properties": {}})
    ]


def test_builtin_tools_exposes_the_expected_set(tmp_path) -> None:  # type: ignore[no-untyped-def]
    tools = builtin_tools(object(), tmp_path)
    names = [tool.name for tool in tools]
    assert names == [
        "read",
        "write",
        "edit",
        "glob",
        "grep",
        "pwsh",
        "bash",
        "web_search",
        "web_fetch",
        "todo_write",
        "present",
    ]
    registry = ToolRegistry(tools)
    assert [schema.name for schema in registry.schemas()] == names
    for schema in registry.schemas():
        assert schema.parameters["type"] == "object"
        assert isinstance(schema.description, str) and schema.description
