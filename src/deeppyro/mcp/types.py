"""Configuration and result types for the MCP client."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

#: Protocol revision deeppyro asks for first.
LATEST_PROTOCOL_VERSION = "2025-06-18"

#: Revision used when the server rejects the latest one.
FALLBACK_PROTOCOL_VERSION = "2024-11-05"

#: DSH namespaces MCP tools as ``mcp__<serverName>__<rawName>``.
SERVER_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"
_SERVER_NAME = re.compile(SERVER_NAME_PATTERN)


class ReconnectConfig(BaseModel):
    """``[mcp.servers.reconnect]``: how hard the supervisor tries to come back."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    initial_delay_ms: int = Field(500, validation_alias=AliasChoices("initial_delay_ms", "initialDelayMs"))
    max_delay_ms: int = Field(30_000, validation_alias=AliasChoices("max_delay_ms", "maxDelayMs"))
    max_attempts: int = Field(10, validation_alias=AliasChoices("max_attempts", "maxAttempts"))

    @model_validator(mode="after")
    def _check(self) -> ReconnectConfig:
        if self.initial_delay_ms <= 0:
            raise ValueError("reconnect.initial_delay_ms must be positive")
        if self.max_delay_ms < self.initial_delay_ms:
            raise ValueError("reconnect.max_delay_ms must be at least reconnect.initial_delay_ms")
        if self.max_attempts <= 0:
            raise ValueError("reconnect.max_attempts must be positive")
        return self


class McpServerConfig(BaseModel):
    """One ``[[mcp.servers]]`` row, mirroring the DSH field names."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str = Field(validation_alias=AliasChoices("name", "server_name", "serverName"))
    transport: Literal["stdio", "streamable-http", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    startup_timeout: float = 20.0
    tool_call_timeout_ms: int = Field(
        60_000, validation_alias=AliasChoices("tool_call_timeout_ms", "toolCallTimeoutMs")
    )
    max_instruction_bytes: int = Field(
        32_768, validation_alias=AliasChoices("max_instruction_bytes", "maxInstructionBytes")
    )
    fail_on_startup_error: bool = Field(
        False, validation_alias=AliasChoices("fail_on_startup_error", "failOnStartupError")
    )
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)

    @model_validator(mode="after")
    def _check_transport(self) -> McpServerConfig:
        if not self.name.strip():
            raise ValueError("mcp server name must not be empty")
        if not _SERVER_NAME.match(self.name):
            raise ValueError(
                f"mcp server name {self.name!r} must match {SERVER_NAME_PATTERN} "
                "(letters, digits, underscore and hyphen, at most 32 characters)"
            )
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError(f"mcp server {self.name!r}: the stdio transport requires a command")
        if self.streamable_http and not self.url.strip():
            raise ValueError(f"mcp server {self.name!r}: the streamable-http transport requires a url")
        if self.tool_call_timeout_ms <= 0:
            raise ValueError("tool_call_timeout_ms must be positive")
        if self.max_instruction_bytes <= 0:
            raise ValueError("max_instruction_bytes must be positive")
        return self

    @property
    def streamable_http(self) -> bool:
        """``http`` is the pre-DSH-alignment spelling of ``streamable-http``."""

        return self.transport in ("streamable-http", "http")

    @property
    def call_timeout(self) -> float:
        """Seconds per ``tools/call`` or resource request."""

        return self.tool_call_timeout_ms / 1000.0


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
    instructions: str = ""
    resources: bool = False
    reconnecting: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "error": self.error,
            "tools": [tool.name for tool in self.tools],
            "instructions": self.instructions,
            "resources": self.resources,
            "reconnecting": self.reconnecting,
        }


__all__ = [
    "FALLBACK_PROTOCOL_VERSION",
    "LATEST_PROTOCOL_VERSION",
    "SERVER_NAME_PATTERN",
    "McpServerConfig",
    "McpServerStatus",
    "McpToolInfo",
    "ReconnectConfig",
]
