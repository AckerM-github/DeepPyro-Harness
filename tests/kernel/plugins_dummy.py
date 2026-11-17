"""Dummy plugins used by kernel tests (never shipped)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from deeppyro.api import Plugin, Service
from deeppyro.core.payload import field_of


class CounterPlugin(Service):
    """Provides `counter`; config: {"start": int}."""

    provide = "counter"

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)
        self.n = int((config or {}).get("start", 0))

    def inc(self) -> int:
        self.n += 1
        return self.n


class ConsumerPlugin:
    """Depends on `counter` and records that it saw the dependency."""

    inject: ClassVar[list[str]] = ["counter"]

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        self.ctx = ctx
        self.config = config or {}
        self.seen: int | None = None

    def init(self) -> None:
        self.seen = self.ctx.get("counter").n


class ExplodingPlugin:
    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        raise RuntimeError("boom")


#: tags of every OrderedPlugin instance, in construction (== mount) order.
MOUNT_ORDER: list[str] = []


class OrderedPlugin(Plugin):
    """api.Plugin that records its construction order; config: {"tag": str}."""

    plugin_id = "ordered"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)
        self.tag = str(field_of(config, "tag", "") or "")
        MOUNT_ORDER.append(self.tag)

    def setup(self) -> None:
        self.ready = True


#: values seen by InterceptProbePlugin.setup (group intercept tests).
OBSERVED: list[int] = []


class InterceptProbePlugin(Plugin):
    """Depends on `counter` and records the value it resolved at setup."""

    plugin_id = "intercept-probe"
    inject: ClassVar[list[Any]] = [CounterPlugin]

    def setup(self) -> None:
        OBSERVED.append(int(self.ctx.require(CounterPlugin).n))


class InterceptAwareService(Service):
    """Service that records its resolved intercept config."""

    provide = "intercept-aware"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.resolved: dict[str, Any] = dict(self.intercept_config or {})


class VolatileConfig(BaseModel):
    threshold: int = 1
    label: str = "default"


class VolatilePlugin(Plugin):
    """api.Plugin with one volatile field (threshold) and an on_config hook."""

    plugin_id = "volatile"
    Config = VolatileConfig
    volatile: ClassVar[tuple[str, ...]] = ("threshold",)

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, config)
        self.hook_calls: list[int] = []

    def on_config(self, config: VolatileConfig) -> None:
        self.hook_calls.append(int(config.threshold))


class NonVolatilePlugin(Plugin):
    """api.Plugin with Config but no volatile fields."""

    plugin_id = "non-volatile"
    Config = VolatileConfig
