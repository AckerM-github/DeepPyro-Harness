"""Attachments plugin wiring: resolves [attachments] config and provides the store."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deeppyro.attachments import AttachmentConfig, AttachmentStore
from deeppyro.builtin.attachments import AttachmentsPlugin


class _FakeContext:
    """Minimal kernel context replacement for direct plugin construction."""

    def __init__(self, config_value: Any = None) -> None:
        self._config_service = SimpleNamespace(value=config_value)
        self.provided: dict[str, Any] = {}

    def get(self, name: str) -> Any:
        return self._config_service if name == "config" else None

    def provide(self, name: str, value: Any) -> None:
        self.provided[name] = value


def test_plugin_provides_store_with_default_config(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path))
    ctx = _FakeContext(SimpleNamespace(attachments=None))
    plugin = AttachmentsPlugin(ctx, {})
    plugin.setup(ctx, {})
    store = ctx.provided["attachments"]
    assert isinstance(store, AttachmentStore)
    assert store.config == AttachmentConfig()
    assert store.directory == tmp_path / "attachments"


def test_plugin_reads_camel_case_attachments_table(tmp_path: Path) -> None:
    table = {
        "directory": str(tmp_path / "custom"),
        "maxImagesPerMessage": 3,
        "maxImageBytes": 1024,
        "maxPixels": 1000,
        "maxDimension": 100,
    }
    ctx = _FakeContext(SimpleNamespace(attachments=table))
    plugin = AttachmentsPlugin(ctx, {})
    plugin.setup(ctx, {})
    store = ctx.provided["attachments"]
    assert store.config.max_images_per_message == 3
    assert store.config.max_image_bytes == 1024
    assert store.directory == tmp_path / "custom"


def test_plugin_row_config_is_overridden_by_user_table(tmp_path: Path) -> None:
    ctx = _FakeContext(SimpleNamespace(attachments={"maxImagesPerMessage": 7}))
    plugin = AttachmentsPlugin(
        ctx,
        {
            "directory": str(tmp_path / "row-dir"),
            "maxImagesPerMessage": 2,
            "maxDimension": 64,
        },
    )
    plugin.setup(ctx, {})
    store = ctx.provided["attachments"]
    assert store.config.max_images_per_message == 7
    assert store.config.max_dimension == 64
    assert store.directory == tmp_path / "row-dir"
