"""Plugins used by the Layer D boot tests.

They live in the tests package so the kernel can resolve them by module:Class
import path; nothing here is imported by the product code.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from deeppyro.api import Plugin


class NestedConfig(BaseModel):
    enabled: bool = True
    threshold: int = 1


class DemoConfig(BaseModel):
    apiKey: str = ""
    model: str = "local"
    maxTokens: int = 4096
    temperature: float = 0.2
    token: str = ""
    nested: NestedConfig = Field(default_factory=NestedConfig)


class DemoPlugin(Plugin):
    plugin_id = "demo-plugin"
    version = "0.2.0"
    description = "config demo plugin"
    Config = DemoConfig
    volatile: ClassVar[tuple[str, ...]] = ("temperature",)

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.started = False

    def setup(self, ctx: Any, config: Any) -> None:
        self.started = True


class NoConfigPlugin(Plugin):
    plugin_id = "no-config"
    version = "0.1.0"
    description = "plugin without a pydantic Config"


class ExplodingPlugin(Plugin):
    plugin_id = "exploding"
    version = "0.1.0"
    description = "plugin that always fails during setup"

    def setup(self, ctx: Any, config: Any) -> None:
        raise RuntimeError("exploding plugin failed on purpose")


__all__ = [
    "DemoConfig",
    "DemoPlugin",
    "ExplodingPlugin",
    "NestedConfig",
    "NoConfigPlugin",
]
