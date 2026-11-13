"""Base classes for DeepPyro plugins and services.

A plugin gets one instance per plugin fiber. Construction and setup may happen
in two phases so a plugin can declare its identity without touching the host:

    class HelloPlugin(api.Plugin):
        plugin_id = hello

        def setup(self, ctx, config):
            ctx.logger.info(hello from s, config.name)

Services are plugins that additionally register a named service:

    class NotesService(api.Service):
        provide = notes

        def __init__(self, ctx, config):
            super().__init__(ctx, config)
            self.items = []

The kernel validates the pydantic Config class before construction, so config
here is already a model instance (or a plain dict when Config is None).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel

from deeppyro._vendor.cordis_port import Service as CordisService

from .errors import PluginError


class Plugin:
    """Base class for a host plugin."""

    api_version: ClassVar[int] = 1
    plugin_id: ClassVar[str] = ""
    version: ClassVar[str] = "0.0.0"
    description: ClassVar[str] = ""
    Config: ClassVar[type[BaseModel] | None] = None
    inject: ClassVar[list[str]] = []

    #: Set by the adapter before setup runs.
    ctx: Any
    config: Any

    def __init__(self, ctx: Any, config: Any) -> None:
        self.ctx = ctx
        self.config = config

    def setup(self, ctx: Any, config: Any) -> Any:
        """Register contributions.

        May be synchronous or asynchronous; the kernel awaits the result when
        needed. Registrations made through ctx are undone automatically when
        the plugin fiber unloads.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.plugin_id!r}>"


class Service(Plugin, CordisService):
    """A plugin that also provides a named Cordis service."""

    provide: ClassVar[str] = ""

    def __init__(self, ctx: Any, config: Any) -> None:
        if not self.provide:
            raise PluginError(f"{type(self).__name__}.provide must name a service")
        raw = getattr(ctx, "raw", ctx)
        # PluginContext proxies unknown attributes to the raw context, so the
        # service keeps access to the full Cordis surface through self.ctx.
        CordisService.__init__(self, raw, name=self.provide)
        Plugin.__init__(self, ctx, config)


__all__ = ["Plugin", "Service"]
