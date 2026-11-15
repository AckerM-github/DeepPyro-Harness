"""Configuration and result types for the MCP client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Protocol revision deeppyro asks for first.
LATEST_PROTOCOL_VERSION = "2025-06-18"

#: Revision used when the server rejects the latest one.
FALLBACK_PROTOCOL_VERSION = "2024-11-05"


class McpServerConfig(BaseModel):
    """One ``[[mcp.servers]]`` row."""

    model_config = ConfigDict(extra="allow")

    name: str
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    startup_timeout: float = 20.0
    call_timeout: float = 60.0

    @model_validator(mode="after")
    def _check_transport(self) -> McpServerConfig:
        if not self.name.strip():
            raise ValueError("mcp server name must not be empty")
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError(f"mcp server {self.name!r}: the stdio transport requires a command")
        if self.transport == "http" and not self.url.strip():
            raise ValueError(f"mcp server {self.name!r}: the http transport requires a url")
        return self


@dataclass(slots=True)
class McpToolInfo:
    """A tool advertised by ``tools/list``."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] | None = None


@dataclass(slots=True)
class McpServerStatus:
    """Last known state of one configured server."""

    name: str
    ok: bool
    error: str = ""
    tools: list[McpToolInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "error": self.error,
            "tools": [tool.name for tool in self.tools],
        }
