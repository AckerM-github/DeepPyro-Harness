"""Bridge MCP tools into deeppyro :class:`BaseTool` instances."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool

from .jsonrpc import JsonRpcError
from .transport import McpTransportError
from .types import McpToolInfo

#: MCP tool names allow any characters; deeppyro tool names do not.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

#: Suffix length of the identity hash DSH appends when normalisation is lossy.
HASH_SUFFIX = 12


def sanitize_name(value: str) -> str:
    """Replace every character outside ``[A-Za-z0-9_-]`` with an underscore."""

    cleaned = _UNSAFE.sub("_", str(value).strip())
    return cleaned or "tool"


def identity_hash(server: str, tool: str) -> str:
    """A stable 12-hex digest of one ``(server, tool)`` identity."""

    digest = hashlib.sha256(f"{server}\x00{tool}".encode()).hexdigest()
    return digest[:HASH_SUFFIX]


def public_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``, hash-suffixed whenever normalisation is lossy.

    DSH guarantees that two distinct identities never collapse onto one public
    name, so a rewritten name carries a digest of the original pair.
    """

    clean_server = sanitize_name(server)
    clean_tool = sanitize_name(tool)
    name = f"mcp__{clean_server}__{clean_tool}"
    if clean_tool != tool:
        name = f"{name}_{identity_hash(server, tool)}"
    return name


def mcp_tool_name(server: str, tool: str) -> str:
    """Backwards-compatible alias of :func:`public_tool_name`."""

    return public_tool_name(server, tool)


class McpToolAdapter(BaseTool):
    """One remote MCP tool exposed to deeppyro's tool registry."""

    #: Remote tools may mutate state; never run two calls of the same batch tool in parallel.
    parallel_safe = False

    def __init__(
        self,
        client: Any,
        tool: McpToolInfo,
        *,
        server: str | None = None,
        attachments: Any = None,
        call: Any = None,
    ) -> None:
        self.client = client
        self._call = call or (lambda name, args: client.call_tool(name, args))
        self.server = server or getattr(getattr(client, "config", None), "name", "server")
        self.logger = logging.getLogger(f"deeppyro.mcp.{self.server}")
        self.remote_name = tool.name
        self.name = public_tool_name(self.server, tool.name)
        self.description = tool.description or f"{tool.name} from MCP server {self.server}"
        self.annotations = tool.annotations
        self._attachments = attachments
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        if schema.get("type") == "object" and isinstance(schema.get("properties"), dict):
            self.parameters = schema
        else:
            self.parameters = {"type": "object", "properties": {}, "additionalProperties": False}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await self._call(self.remote_name, dict(args or {}))
        except JsonRpcError as exc:
            raise ToolError(f"mcp tool {self.name} failed: {exc.message}") from exc
        except McpTransportError as exc:
            raise ToolError(f"mcp tool {self.name} failed: {exc}") from exc
        text = flatten_content(result)
        if result.get("isError"):
            raise ToolError(text or f"mcp tool {self.name} reported an error")
        parts = await self._image_parts(result)
        return ToolResult(
            text=text, parts=parts, meta={"mcp": {"server": self.server, "tool": self.remote_name}}
        )

    async def _image_parts(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Persist image blocks when the host has an attachment store."""

        images = image_blocks(result)
        if not images or self._attachments is None:
            return []
        parts: list[dict[str, Any]] = []
        for data, media_type in images:
            try:
                ref = await self._attachments.save_image(data, media_type=media_type)
            except Exception as exc:  # noqa: BLE001 - a rejected image is a diagnostic
                self.logger.warning("mcp image from %s was rejected: %s", self.server, exc)
                continue
            parts.append({"type": "image", "attachment": ref.id, "mediaType": media_type})
        return parts


def image_blocks(result: dict[str, Any]) -> list[tuple[bytes, str]]:
    """Decode the ``image`` content blocks of one MCP result."""

    images: list[tuple[bytes, str]] = []
    content = result.get("content")
    if not isinstance(content, list):
        return images
    for item in content:
        if not isinstance(item, dict) or str(item.get("type") or "") != "image":
            continue
        data = item.get("data")
        mime = str(item.get("mimeType") or "application/octet-stream")
        if not isinstance(data, str) or mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            continue
        try:
            images.append((base64.b64decode(data, validate=True), mime))
        except (binascii.Error, ValueError):
            continue
    return images


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
            mime = item.get("mimeType") or "unknown type"
            parts.append(f"[image: {mime}]")
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


def render_resource_result(server: str, result: dict[str, Any]) -> str:
    """Render ``resources/read`` output with server attribution and no inline binary."""

    rendered = _strip_blobs(result)
    body = json.dumps(rendered, ensure_ascii=False, indent=2)
    return f"MCP server: {server}\n{body}"


def _strip_blobs(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key == "blob" and isinstance(item, str):
                cleaned[key] = (
                    f"[binary resource: {len(item)} base64 characters; available to programmatic callers]"
                )
            else:
                cleaned[key] = _strip_blobs(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_blobs(item) for item in value]
    return value


__all__ = [
    "McpToolAdapter",
    "flatten_content",
    "identity_hash",
    "image_blocks",
    "mcp_tool_name",
    "public_tool_name",
    "render_resource_result",
    "sanitize_name",
]
