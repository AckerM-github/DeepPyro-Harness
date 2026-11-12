"""The kernel-owned `config` service.

Every plugin may inject `config` to read the validated deeppyro configuration
(providers, models, agent defaults). Keeping it a cordis service means plugins
never import the global config loader directly, so tests can substitute config.
"""

from __future__ import annotations

from typing import Any

from deeppyro._vendor.cordis_port import Service
from deeppyro.config import Config


class KernelConfig(Service):
    """Validated configuration exposed as the `config` service."""

    provide = "config"

    def __init__(self, ctx: Any, value: Config | dict[str, Any] | None = None) -> None:
        super().__init__(ctx)
        if value is None:
            self.value = load_config()
        elif isinstance(value, Config):
            self.value = value
        else:
            self.value = Config.model_validate(value)


def provide_config(ctx: Any, value: Config | dict[str, Any] | None = None) -> KernelConfig:
    """Register the `config` service on `ctx` (root kernel bootstrap)."""
    return KernelConfig(ctx, value)
