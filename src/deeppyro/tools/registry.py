"""Tool registry: registration, lookup, and model-facing schemas."""

from __future__ import annotations

from collections.abc import Iterable

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import Tool, ToolSchema


class ToolRegistry:
    """Ordered collection of tools.

    Registration order is the order the model sees in `schemas()`, so a
    session should register tools in the order it wants them advertised.
    Re-registering an existing name raises :class:`ToolError` rather than
    silently shadowing a tool.
    """

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        """Register `tool`, raising when the name is already taken."""
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name:
            raise ToolError("tool must declare a non-empty name")
        if name in self._tools:
            raise ToolError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        """Return the tool registered under `name`, or None."""
        return self._tools.get(name)

    def schemas(self) -> list[ToolSchema]:
        """Return model-facing schemas in registration order."""
        schemas: list[ToolSchema] = []
        for tool in self._tools.values():
            schemas.append(
                ToolSchema(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                )
            )
        return schemas

    def names(self) -> list[str]:
        """Return registered tool names in registration order."""
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._tools.values())
