"""The kernel-owned `config` service.

Every plugin may inject `config` to read the validated deeppyro configuration
(providers, models, agent defaults). Keeping it a kernel service means plugins
never import the global config loader directly, so tests can substitute config.
"""

from __future__ import annotations

from typing import Any, cast

from deeppyro.api.context import PluginContext
from deeppyro.api.plugin import Service
from deeppyro.config import Config, load_config
from deeppyro.kernel.context import RuntimeContext


class KernelConfig(Service):
    """Validated configuration exposed as the `config` service."""

    provide = "config"

    def __init__(self, ctx: PluginContext, value: Config | dict[str, Any] | None = None) -> None:
        super().__init__(ctx)
        if value is None:
            self.value = load_config()
        elif isinstance(value, Config):
            self.value = value
        else:
            self.value = Config.model_validate(value)


def provide_config(ctx: RuntimeContext, value: Config | dict[str, Any] | None = None) -> KernelConfig:
    """Register the `config` service on `ctx` (root kernel bootstrap)."""
    # The root config service is constructed before any PluginContext facade
    # exists. The cast is typing-only: the service still receives the raw
    # runtime context exactly as before.
    return KernelConfig(cast("PluginContext", ctx), value)
