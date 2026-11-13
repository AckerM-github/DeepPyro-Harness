"""Kernel attachments plugin: provides the image attachment store as a service.

The store itself lives in the attachments package and has no kernel
dependencies, so SDK code and tests can use it directly. This plugin resolves
the [attachments] table from the kernel config (with the bundle row config as
a fallback), registers the store under the attachments service name, and does
nothing else. Image offload (request budgeting and projection) is phase 2 and
lives in the llm and session layers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.attachments import AttachmentStore


def _attachments_config(ctx: Any, row_config: Any) -> dict[str, Any]:
    """Merge the bundle row config under the [attachments] table from the kernel config."""
    merged: dict[str, Any] = {}
    if isinstance(row_config, Mapping):
        merged.update(row_config)
    config_service = ctx.get("config")
    value = getattr(config_service, "value", None) if config_service is not None else None
    raw: Any = None
    if value is not None:
        raw = getattr(value, "attachments", None)
        if raw is None and isinstance(value, Mapping):
            raw = value.get("attachments")
    if isinstance(raw, Mapping):
        merged.update(raw)
    else:
        model_dump = getattr(raw, "model_dump", None)
        if callable(model_dump):
            merged.update(model_dump())
    return merged


class AttachmentsPlugin(Plugin):
    """Exposes the AttachmentStore as the attachments service."""

    provide = "attachments"
    plugin_id = "attachments"
    version = "0.1.0"
    description = "image attachment storage (validation, normalization, references)"
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.store = AttachmentStore(config=_attachments_config(ctx, config))

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("attachments", self.store)


__all__ = ["AttachmentsPlugin"]
