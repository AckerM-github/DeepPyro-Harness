"""Model Context Protocol (MCP) client support.

Only the client half of MCP is implemented: connecting to servers over stdio or
streamable HTTP and bridging their tools into the deeppyro tool registry.
Resources, prompts, sampling and elicitation are deliberate non-goals; see
``docs/spec/mcp.md``.
"""

from __future__ import annotations

from .bridge import McpToolAdapter, mcp_tool_name, sanitize_name
from .client import McpClient
from .jsonrpc import JsonRpcError, McpProtocolError
from .service import McpService, parse_servers
from .transport import HttpTransport, McpTimeoutError, McpTransportError, StdioTransport
from .types import McpServerConfig, McpServerStatus, McpToolInfo

__all__ = [
    "HttpTransport",
    "JsonRpcError",
    "McpClient",
    "McpProtocolError",
    "McpServerConfig",
    "McpServerStatus",
    "McpService",
    "McpTimeoutError",
    "McpToolAdapter",
    "McpToolInfo",
    "McpTransportError",
    "StdioTransport",
    "mcp_tool_name",
    "parse_servers",
    "sanitize_name",
]
