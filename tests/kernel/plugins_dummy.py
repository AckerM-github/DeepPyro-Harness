"""Dummy cordis plugins used by kernel tests (never shipped)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from deeppyro._vendor.cordis_port import Service
from deeppyro.api import Plugin


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
        self.seen = self.ctx.counter.n


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
        if isinstance(config, dict):
            tag = config.get("tag", "")
        else:
            tag = getattr(config, "tag", "")
        self.tag = str(tag or "")
        MOUNT_ORDER.append(self.tag)

    def setup(self, ctx: Any, config: Any) -> None:
        self.ready = True


#: values seen by InterceptProbePlugin.setup (group intercept tests).
OBSERVED: list[int] = []


class InterceptProbePlugin(Plugin):
    """Depends on `counter` and records the value it resolved at setup."""

    plugin_id = "intercept-probe"
    inject: ClassVar[list[str]] = ["counter"]

    def setup(self, ctx: Any, config: Any) -> None:
        OBSERVED.append(int(ctx.counter.n))


class InterceptAwareService(Service):
    """Cordis service that records its resolved intercept config."""

    provide = "intercept-aware"

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self.resolved: dict[str, Any] = dict(self.__resolve_intercept__() or {})


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
