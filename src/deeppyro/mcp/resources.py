"""The three shared MCP resource tools, mirroring ``dsh-mcp-resources``."""

from __future__ import annotations

import json
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool, make_parameters

from .bridge import render_resource_result
from .service import McpService


class _ResourceTool(BaseTool):
    """Shared plumbing: resolve the server by name, then call it."""

    parallel_safe = True

    def __init__(self, service: McpService) -> None:
        self.service = service

    def _server(self, args: dict[str, Any]) -> str:
        server = str(args.get("server") or "").strip()
        if not server:
            raise ToolError(f"{self.name} requires a server argument")
        if self.service.state(server) is None:
            known = ", ".join(self.service.server_names()) or "(none configured)"
            raise ToolError(f"unknown mcp server {server!r}; configured servers: {known}")
        return server

    def _cursor(self, args: dict[str, Any]) -> str | None:
        cursor = args.get("cursor")
        return str(cursor) if isinstance(cursor, str) and cursor else None

    @staticmethod
    def _pages(server: str, page: dict[str, Any]) -> str:
        body = json.dumps(page, ensure_ascii=False, indent=2)
        return f"MCP server: {server}\n{body}"


class ListResourcesTool(_ResourceTool):
    name = "list_mcp_resources"
    description = (
        "List the resources one MCP server exposes. Pass the server name; "
        "omit cursor for the first page and echo nextCursor back to continue."
    )

    def __init__(self, service: McpService) -> None:
        super().__init__(service)
        self.parameters = make_parameters(
            {
                "server": {"type": "string", "description": "MCP server name"},
                "cursor": {"type": "string", "description": "nextCursor from a previous page"},
            },
            required=["server"],
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        server = self._server(args)
        try:
            page = await self.service.list_resources(server, self._cursor(args))
        except Exception as exc:
            raise ToolError(f"list_mcp_resources failed for {server}: {exc}") from exc
        return ToolResult(
            text=self._pages(server, page), meta={"mcp": {"server": server, "resource": "list"}}
        )


class ListResourceTemplatesTool(_ResourceTool):
    name = "list_mcp_resource_templates"
    description = (
        "List the resource templates one MCP server exposes. Pass the server name; "
        "omit cursor for the first page and echo nextCursor back to continue."
    )

    def __init__(self, service: McpService) -> None:
        super().__init__(service)
        self.parameters = make_parameters(
            {
                "server": {"type": "string", "description": "MCP server name"},
                "cursor": {"type": "string", "description": "nextCursor from a previous page"},
            },
            required=["server"],
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        server = self._server(args)
        try:
            page = await self.service.list_resource_templates(server, self._cursor(args))
        except Exception as exc:
            raise ToolError(f"list_mcp_resource_templates failed for {server}: {exc}") from exc
        return ToolResult(
            text=self._pages(server, page),
            meta={"mcp": {"server": server, "resource": "templates"}},
        )


class ReadResourceTool(_ResourceTool):
    name = "read_mcp_resource"
    description = "Read one MCP resource by the uri listed for its server."

    def __init__(self, service: McpService) -> None:
        super().__init__(service)
        self.parameters = make_parameters(
            {
                "server": {"type": "string", "description": "MCP server name"},
                "uri": {"type": "string", "description": "Resource uri, template-expanded when needed"},
            },
            required=["server", "uri"],
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        server = self._server(args)
        uri = str(args.get("uri") or "").strip()
        if not uri:
            raise ToolError("read_mcp_resource requires a uri argument")
        try:
            result = await self.service.read_resource(server, uri)
        except Exception as exc:
            raise ToolError(f"read_mcp_resource failed for {server}: {exc}") from exc
        return ToolResult(
            text=render_resource_result(server, result),
            meta={"mcp": {"server": server, "resource": uri}},
        )


def resource_tools(service: McpService) -> list[BaseTool]:
    """The three shared tools, or nothing when no server is configured."""

    if not service.server_names():
        return []
    return [ListResourcesTool(service), ListResourceTemplatesTool(service), ReadResourceTool(service)]


__all__ = [
    "ListResourceTemplatesTool",
    "ListResourcesTool",
    "ReadResourceTool",
    "resource_tools",
]
