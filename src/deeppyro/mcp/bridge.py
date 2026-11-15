"""Bridge MCP tools into deeppyro :class:`BaseTool` instances."""

from __future__ import annotations

import re
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool, make_parameters

from .jsonrpc import JsonRpcError
from .transport import McpTransportError
from .types import McpToolInfo

#: MCP tool names allow any characters; deeppyro tool names do not.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_name(value: str) -> str:
    """Replace every character outside ``[A-Za-z0-9_-]`` with an underscore."""

    cleaned = _UNSAFE.sub("_", str(value).strip())
    return cleaned or "tool"


def mcp_tool_name(server: str, tool: str) -> str:
    """The deeppyro tool name for one MCP tool."""

    return f"mcp__{sanitize_name(server)}__{sanitize_name(tool)}"


class McpToolAdapter(BaseTool):
    """One remote MCP tool exposed to deeppyro's tool registry."""

    #: Remote tools may mutate state; never run two calls of the same batch tool in parallel.
    parallel_safe = False

    def __init__(self, client: Any, tool: McpToolInfo, *, server: str | None = None) -> None:
        self.client = client
        self.server = server or getattr(getattr(client, "config", None), "name", "server")
        self.remote_name = tool.name
        self.name = mcp_tool_name(self.server, tool.name)
        self.description = tool.description or f"{tool.name} from MCP server {self.server}"
        self.annotations = tool.annotations
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
            self.parameters = schema
        else:
            self.parameters = make_parameters({})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await self.client.call_tool(self.remote_name, dict(args or {}))
        except JsonRpcError as exc:
            raise ToolError(f"mcp tool {self.name} failed: {exc.message}") from exc
        except McpTransportError as exc:
            raise ToolError(f"mcp tool {self.name} failed: {exc}") from exc
        text = flatten_content(result)
        if result.get("isError"):
            raise ToolError(text or f"mcp tool {self.name} reported an error")
        return ToolResult(text=text, meta={"mcp": {"server": self.server, "tool": self.remote_name}})


def flatten_content(result: dict[str, Any]) -> str:
    """Turn an MCP ``content`` list into plain text for the model."""

    content = result.get("content")
    if not isinstance(content, list):
        return "(no content)"
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "image":
            parts.append(f"[image: {item.get('mimeType') or 'unknown type'}]")
        elif kind == "audio":
            parts.append("[audio content]")
        elif kind == "resource":
            resource = item.get("resource")
            uri = resource.get("uri") if isinstance(resource, dict) else None
            parts.append(f"[resource: {uri or 'unknown'}]")
        else:
            parts.append(f"[{kind or 'unknown'} content]")
    text = "\n".join(part for part in parts if part)
    return text or "(no content)"


__all__ = ["McpToolAdapter", "flatten_content", "mcp_tool_name", "sanitize_name"]
