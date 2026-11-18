"""Registers the core system prompt sections on the prompt service."""

from __future__ import annotations

from typing import ClassVar

from deeppyro.api import Plugin
from deeppyro.prompt.prompt import core_sections


class PromptCorePlugin(Plugin):
    """Default prompt content: identity, environment, instructions, tools, rules."""

    inject: ClassVar[list[str]] = ["prompt"]
    plugin_id = "prompt-core"
    version = "0.2.0"
    description = "core system prompt sections"

    def setup(self) -> None:
        for name, order, renderer in core_sections():
            self.ctx.register_prompt_section(name, order, renderer)
