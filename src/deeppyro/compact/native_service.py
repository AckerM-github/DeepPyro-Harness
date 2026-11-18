"""Service and plugin entry points for native compaction.

The native compactor itself lives in native_compactor (automatic, one
compaction.done fact) and native_compress (model-authored blocks).  This module
is the seam the TUI and headless runtimes call:

* NativeCompactService is a plain object a runtime can build from a provider, a
  model and a sidecar root.  It exposes should_compact (the pre-send policy),
  native_compact (manual /compact), native_compress (the compress tool path) and
  pre_send_compact (run the policy then compact).
* NativeCompactPlugin mounts as the "compact" service, resolves the sidecar root
  from the sessions store, and attaches to a provider on demand.

Neither path is wired into agent/loop.py in this wave; the pre-send policy is a
pure callable (should_compact_before_send) a later TurnLayer hook can call.
"""

from __future__ import annotations

import pathlib
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from pydantic import BaseModel

from deeppyro.api import PluginConfig, PluginContext, Service
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.compact.blocks import BlockStore
from deeppyro.compact.native_compactor import (
    CompactionDone,
    NativeCompactor,
    should_compact_before_send,
)
from deeppyro.compact.native_compress import NativeCompressor, NativeCompressOutcome
from deeppyro.compact.native_refs import RefStore
from deeppyro.core.errors import ConfigError
from deeppyro.core.llm import LlmProvider, ToolSpec
from deeppyro.session.fold import SessionView, fold
from deeppyro.session.schema import NativeSession


def session_view(session: NativeSession | object) -> SessionView:
    """Fold a NativeSession or NativeSessionHandle-shaped adapter into a view."""
    native = getattr(session, "native_session", session)
    if not isinstance(native, NativeSession):
        raise TypeError("native compaction requires a NativeSession or a native session handle")
    return fold(native.header, native.events)


def _store_root(service: Any) -> pathlib.Path | None:
    store = getattr(service, "store", service)
    root = getattr(store, "root", None)
    return pathlib.Path(root) if root is not None else None


class NativeCompactService:
    """Compaction facade shared by the TUI, headless runs and tests."""

    def __init__(
        self,
        provider: LlmProvider,
        model: str,
        *,
        blocks: BlockStore | None = None,
        refs: RefStore | None = None,
        blocks_root: str | pathlib.Path | None = None,
        refs_root: str | pathlib.Path | None = None,
        keep_recent_tokens: int = 8000,
        context_window: int = 32768,
        threshold: float = 0.85,
        max_tokens: int | None = None,
        trace_store: Any | None = None,
    ) -> None:
        if blocks is None:
            if blocks_root is None:
                raise ConfigError("native compaction needs a blocks store or a blocks root")
            root = pathlib.Path(blocks_root)
            blocks = BlockStore(root)
            if refs is None:
                refs = RefStore(pathlib.Path(refs_root) if refs_root is not None else root)
        if refs is None:
            raise ConfigError("native compaction needs a refs store")
        self.provider = provider
        self.model = model
        self.blocks = blocks
        self.refs = refs
        self.compactor = NativeCompactor(
            provider,
            model,
            keep_recent_tokens=keep_recent_tokens,
            context_window=context_window,
            threshold=threshold,
            max_tokens=max_tokens,
            blocks=blocks,
            refs=refs,
            trace_store=trace_store,
        )
        self.compressor = NativeCompressor(blocks, refs)

    # -- policy ------------------------------------------------------------
    @property
    def context_window(self) -> int:
        return self.compactor.context_window

    @property
    def threshold(self) -> float:
        return self.compactor.threshold

    def should_compact(self, view_or_messages: SessionView | Sequence[Any]) -> bool:
        """Pre-send policy: True when the context should be compacted before sending."""
        return self.compactor.should_compact(view_or_messages)

    def presend_should_compact(self, view_or_messages: SessionView | Sequence[Any]) -> bool:
        """Alias the runtime pre-send hook may call."""
        return self.should_compact(view_or_messages)

    # -- actions -----------------------------------------------------------
    async def native_compact(
        self,
        session: NativeSession | object,
        *,
        view: SessionView | None = None,
        tools: Sequence[ToolSpec] = (),
        envelope: Mapping[str, Any] | None = None,
    ) -> CompactionDone | None:
        """Manual compaction entry point (/compact); one fact, or None."""
        return await self.compactor.compact(session, view=view, tools=tools, envelope=envelope)

    async def compact(
        self,
        session: NativeSession | object,
        *,
        view: SessionView | None = None,
        tools: Sequence[ToolSpec] = (),
        envelope: Mapping[str, Any] | None = None,
    ) -> CompactionDone | None:
        """Alias for native_compact."""
        return await self.native_compact(session, view=view, tools=tools, envelope=envelope)

    def native_compress(
        self,
        session: NativeSession | object,
        content: str,
        *,
        envelope: Mapping[str, Any] | None = None,
    ) -> NativeCompressOutcome:
        """Compress tool entry point for model-authored blocks."""
        return self.compressor.compress(session, content, envelope=envelope)

    async def pre_send_compact(
        self,
        session: NativeSession | object,
        *,
        view: SessionView | None = None,
        tools: Sequence[ToolSpec] = (),
        envelope: Mapping[str, Any] | None = None,
    ) -> CompactionDone | None:
        """Apply the pre-send policy, then compact when over budget.

        This is the exact callable a later TurnLayer hook would make before
        sending a request.  It stays unwired in this wave.
        """
        current = view if view is not None else session_view(session)
        if not self.should_compact(current):
            return None
        return await self.native_compact(session, view=current, tools=tools, envelope=envelope)


class NativeCompactConfig(BaseModel):
    """Configuration for the native compact service row."""

    enabled: bool = True
    blocks_dir: str | None = None
    keep_recent_tokens: int = 8000
    context_window: int = 32768
    threshold: float = 0.85
    max_tokens: int | None = None


def _coerce_config(value: PluginConfig) -> NativeCompactConfig:
    """Normalize the host-provided config row into the typed model."""
    if isinstance(value, NativeCompactConfig):
        return value
    if isinstance(value, BaseModel):
        return NativeCompactConfig(**value.model_dump())
    if isinstance(value, Mapping):
        return NativeCompactConfig(**dict(value))
    return NativeCompactConfig()


class NativeCompactPlugin(Service):
    """Mount native compaction as the "compact" service."""

    plugin_id = "native-compact"
    provide = "compact"
    version = "0.1.0"
    description = "native compaction (manual /compact, pre-send policy, compress path)"
    Config = NativeCompactConfig
    inject: ClassVar[list[str]] = ["config", "sessions"]

    def __init__(self, ctx: PluginContext, config: PluginConfig = None) -> None:
        super().__init__(ctx, config)
        self.blocks: BlockStore | None = None
        self.refs: RefStore | None = None
        self._service: NativeCompactService | None = None

    def _config(self) -> NativeCompactConfig:
        return _coerce_config(self.config)

    def setup(self) -> None:
        cfg = self._config()
        if not cfg.enabled:
            return
        sessions = self.ctx.require(SessionsPlugin)
        store_root = _store_root(sessions)
        if cfg.blocks_dir is not None:
            root = pathlib.Path(cfg.blocks_dir)
        elif store_root is not None:
            root = store_root / "native-blocks"
        else:
            raise ConfigError("native compaction requires compact.blocksDir or a sessions store root")
        self.blocks = BlockStore(root)
        self.refs = RefStore(root)

    def attach(
        self,
        provider: LlmProvider,
        model: str,
        *,
        trace_store: Any | None = None,
    ) -> NativeCompactService:
        """Bind a provider/model and return the shared service facade."""
        if self.blocks is None or self.refs is None:
            raise ConfigError("native compact plugin is disabled")
        cfg = self._config()
        self._service = NativeCompactService(
            provider,
            model,
            blocks=self.blocks,
            refs=self.refs,
            keep_recent_tokens=cfg.keep_recent_tokens,
            context_window=cfg.context_window,
            threshold=cfg.threshold,
            max_tokens=cfg.max_tokens,
            trace_store=trace_store,
        )
        return self._service

    @property
    def service(self) -> NativeCompactService:
        if self._service is None:
            raise ConfigError("native compact plugin has no attached provider/model yet")
        return self._service

    def should_compact(self, view_or_messages: SessionView | Sequence[Any]) -> bool:
        """Policy without a provider: the config window/threshold answer it."""
        cfg = self._config()
        return should_compact_before_send(
            view_or_messages,
            context_window=cfg.context_window,
            threshold=cfg.threshold,
        )

    async def native_compact(
        self,
        session: NativeSession | object,
        *,
        view: SessionView | None = None,
        tools: Sequence[ToolSpec] = (),
        envelope: Mapping[str, Any] | None = None,
    ) -> CompactionDone | None:
        return await self.service.native_compact(session, view=view, tools=tools, envelope=envelope)

    def native_compress(
        self,
        session: NativeSession | object,
        content: str,
        *,
        envelope: Mapping[str, Any] | None = None,
    ) -> NativeCompressOutcome:
        return self.service.native_compress(session, content, envelope=envelope)

    async def pre_send_compact(
        self,
        session: NativeSession | object,
        *,
        view: SessionView | None = None,
        tools: Sequence[ToolSpec] = (),
        envelope: Mapping[str, Any] | None = None,
    ) -> CompactionDone | None:
        return await self.service.pre_send_compact(session, view=view, tools=tools, envelope=envelope)


__all__ = [
    "NativeCompactConfig",
    "NativeCompactPlugin",
    "NativeCompactService",
    "session_view",
    "should_compact_before_send",
]
