"""Append configured text to the assembled system prompt.

This is the reference post-order consumer of the system-prompt/assemble
waterfall: the runtime assembles the prompt, then this listener receives the
downstream result through next_step() and returns it with the configured
extension appended.  It never rewrites the base prompt, so disabling the plugin
(or leaving [prompt].extension empty) leaves the assembled text untouched.

Configuration lives in the top-level [prompt] table:

    [prompt]
    extension = ""      # extra text appended after the rendered prompt

Waterfall polarity matters: a handler that returns None without awaiting
next_step() vetoes the dispatch.  This consumer always awaits the chain and
returns its value unchanged unless the extension is non-empty text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.core.errors import ConfigError
from deeppyro.hooks import SYSTEM_PROMPT_ASSEMBLE
from deeppyro.hooks.types import HookNext, HookPayload


class PromptExtensionPlugin(Plugin):
    """Append [prompt].extension to the rendered system prompt."""

    plugin_id = "prompt-extension"
    version = "0.1.0"
    description = "append configured text to the assembled system prompt"
    inject: ClassVar[list[str]] = ["hooks", "config"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.service("config").value
        self._text = _extension_text(cfg)

    @property
    def text(self) -> str:
        """The configured extension text (empty when the plugin is inactive)."""
        return self._text

    def setup(self, ctx: Any, config: Any) -> None:
        hooks = ctx.service("hooks")
        ctx.effect(
            lambda: hooks.on(SYSTEM_PROMPT_ASSEMBLE, self._on_assemble),
            label="prompt-extension",
        )

    async def _on_assemble(self, payload: HookPayload, next_step: HookNext) -> Any:
        """Run the downstream chain first, then append the extension text."""
        rendered = await next_step()
        text = self._text.strip()
        if not isinstance(rendered, str) or not text:
            return rendered
        return f"{rendered}\n\n{text}"


def _extension_text(cfg: Any) -> str:
    """Read [prompt].extension; a missing table or key means no text."""
    table = _section(cfg, "prompt")
    if table is None:
        return ""
    raw = table.get("extension")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ConfigError(f"[prompt] extension must be a string, got {type(raw).__name__}")
    return raw


def _section(cfg: Any, name: str) -> Mapping[str, Any] | None:
    """Return one optional config table as a mapping."""
    if isinstance(cfg, Mapping):
        value = cfg.get(name)
    else:
        value = getattr(cfg, name, None)
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, Mapping):
            return data
    raise ConfigError(f"[{name}] must be a table, got {type(value).__name__}")


__all__ = ["PromptExtensionPlugin"]
