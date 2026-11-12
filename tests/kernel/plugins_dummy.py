"""Dummy cordis plugins used by kernel tests (never shipped)."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro._vendor.cordis_port import Service


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
