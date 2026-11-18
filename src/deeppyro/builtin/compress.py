"""Builtin compress plugin: registers the compress and retrieval tools.

The plugin is intentionally thin. It resolves the per-session block directory,
builds the shared BlockStore/RefStore, and registers the tool factories:
compress writes summaries on the live native session, search_context finds a
keyword in stored summaries, and decompress expands one known block. The tools
receive the live session through ToolContext.services at call time; the
retrieval tools only need the session id because block files are
session-addressed.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import BaseModel

from deeppyro.api import Plugin, PluginConfig, PluginContext
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.compact.blocks import BlockStore
from deeppyro.compact.native_block_tools import CompressTool, DecompressTool, SearchContextTool
from deeppyro.compact.native_refs import RefStore
from deeppyro.core.errors import ConfigError
from deeppyro.kernel.config_service import KernelConfig


class CompressConfig(BaseModel):
    """Configuration for the compress tool (row-level)."""

    blocksDir: str | None = None
    compressEnabled: bool = True


def _global_config(ctx: PluginContext) -> Any:
    try:
        service = ctx.require(KernelConfig)
    except (AttributeError, LookupError):
        return None
    return getattr(service, "value", None)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _section(global_config: Any) -> Mapping[str, Any]:
    mapping = _as_mapping(global_config)
    for key in ("compact", "compress"):
        section = mapping.get(key)
        if isinstance(section, Mapping):
            return section
    return {}


def _pick(section: Mapping[str, Any], row: Mapping[str, Any], *names: str) -> Any:
    for source in (section, row):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
    return None


def _resolve_blocks_dir(
    section: Mapping[str, Any], row: Mapping[str, Any], store_root: pathlib.Path
) -> pathlib.Path:
    raw = _pick(section, row, "blocksDir", "blocks_dir")
    if raw is None:
        return store_root / "blocks"
    if not isinstance(raw, (str, pathlib.Path)):
        raise ConfigError("compact.blocksDir must be a path string")
    path = pathlib.Path(raw)
    if not str(path).strip():
        raise ConfigError("compact.blocksDir must not be empty")
    return path


def _resolve_enabled(section: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    raw = _pick(section, row, "compressEnabled", "compress_enabled")
    if raw is None:
        return True
    if not isinstance(raw, bool):
        raise ConfigError("compact.compressEnabled must be a boolean")
    return raw


def _store_root(service: Any) -> pathlib.Path | None:
    root = getattr(service, "root", None)
    return pathlib.Path(root) if root is not None else None


class CompressPlugin(Plugin):
    """Register the compress tool backed by a per-session block store."""

    plugin_id = "compress"
    version = "0.3.0"
    description = "model-authored context compression blocks and checkpoints"
    Config = CompressConfig
    inject: ClassVar[list[str]] = ["config", "sessions", "tools"]

    def __init__(self, ctx: PluginContext, config: PluginConfig = None) -> None:
        super().__init__(ctx, config)
        self.blocks: BlockStore | None = None
        self.refs: RefStore | None = None

    def setup(self) -> None:
        cfg = self.config
        row = _as_mapping(cfg)
        section = _section(_global_config(self.ctx))
        if not _resolve_enabled(section, row):
            return
        sessions = self.ctx.require(SessionsPlugin)
        store_root = _store_root(sessions)
        if store_root is None:
            raise ConfigError("compress requires the sessions service to expose its root")
        blocks_root = _resolve_blocks_dir(section, row, store_root)
        blocks = BlockStore(blocks_root)
        refs = RefStore(blocks_root)
        self.blocks = blocks
        self.refs = refs
        self.ctx.register_tool(lambda _cfg, _workspace: [CompressTool(blocks, refs)])
        # Retrieval over the same block store: search first, decompress only the hit.
        self.ctx.register_tool(lambda _cfg, _workspace: [SearchContextTool(blocks), DecompressTool(blocks)])


__all__ = ["CompressConfig", "CompressPlugin"]
