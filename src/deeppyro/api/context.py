"""The context object handed to every plugin."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .errors import PluginDependencyError


class PluginContext:
    """A thin, plugin-friendly facade over the raw Cordis context.

    Attributes not defined here are looked up on the raw context, so plugin
    authors can still use events, fibers and the runtime directly. The
    registration helpers add one important behaviour: whatever they register
    is torn down when the plugin fiber unloads.
    """

    __slots__ = ("_logger", "_raw", "config", "plugin_id")

    def __init__(self, raw: Any, *, plugin_id: str = "", config: Any = None) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "_logger", None)

    def __getattr__(self, name: str) -> Any:
        try:
            raw = object.__getattribute__(self, "_raw")
        except AttributeError:  # pragma: no cover - defensive
            raise AttributeError(name) from None
        return getattr(raw, name)

    @property
    def raw(self) -> Any:
        return self._raw

    @property
    def logger(self) -> logging.Logger:
        if self._logger is None:
            name = f"deeppyro.plugin.{self.plugin_id}" if self.plugin_id else "deeppyro.plugin"
            object.__setattr__(self, "_logger", logging.getLogger(name))
        return self._logger

    # -- service access ---------------------------------------------------
    def get(self, name: str) -> Any | None:
        reflect = getattr(self._raw, "reflect", None)
        if reflect is None:
            return None
        return reflect.get(name)

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def service(self, name: str) -> Any:
        value = self.get(name)
        if value is None:
            raise PluginDependencyError(
                f"plugin {self.plugin_id!r} requires service {name!r}, which is not available"
            )
        return value

    @property
    def tools(self) -> Any:
        return self.service("tools")

    @property
    def prompt(self) -> Any:
        return self.service("prompt")

    @property
    def commands(self) -> Any:
        return self.service("commands")

    @property
    def llm(self) -> Any:
        return self.service("llm")

    @property
    def presets(self) -> Any:
        return self.service("presets")

    # -- lifecycle helpers ------------------------------------------------
    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Any:
        return self._raw.effect(execute, label=f"{self.plugin_id}:{label}")

    def on(self, event: Any, handler: Any, options: Any = None) -> Any:
        return self._raw.on(event, handler, options)

    def once(self, event: Any, handler: Any, options: Any = None) -> Any:
        return self._raw.once(event, handler, options)

    def emit(self, *args: Any) -> Any:
        return self._raw.emit(*args)

    def plugin(self, plugin: Any, config: Any = None) -> Any:
        return self._raw.plugin(plugin, config)

    def inject(self, inject: Any, callback: Any) -> Any:
        return self._raw.inject(inject, callback)

    def isolate(self, name: str, label: Any = None) -> PluginContext:
        raw = self._raw.isolate(name, label)
        return PluginContext(raw, plugin_id=self.plugin_id, config=self.config)

    def intercept(self, name: str, config: Any) -> PluginContext:
        raw = self._raw.intercept(name, config)
        return PluginContext(raw, plugin_id=self.plugin_id, config=self.config)

    # -- typed registration ----------------------------------------------
    def _own(self, dispose: Callable[[], Any], label: str) -> Callable[[], Any]:
        self.effect(lambda: dispose, label=label)
        return dispose

    def register_tool(self, factory: Any, *, scope: str | None = None) -> Callable[[], Any]:
        dispose = self.tools.add(factory, scope=scope, plugin_id=self.plugin_id)
        return self._own(dispose, label="tool")

    def register_prompt_section(
        self, name: str, order: int, renderer: Any, *, scope: str | None = None
    ) -> Callable[[], Any]:
        dispose = self.prompt.add_section(name, order, renderer, plugin_id=self.plugin_id, scope=scope)
        return self._own(dispose, label=f"prompt-section:{name}")

    def register_preset(self, preset: Any) -> Callable[[], Any]:
        dispose = self.presets.add(preset, plugin_id=self.plugin_id)
        return self._own(dispose, label=f"preset:{getattr(preset, 'id', preset)}")

    def register_command(
        self,
        name: str,
        handler: Any,
        *,
        summary: str = "",
        usage: str = "",
        aliases: Any = (),
    ) -> Callable[[], Any]:
        dispose = self.commands.add(
            name,
            handler,
            summary=summary,
            usage=usage,
            aliases=tuple(aliases or ()),
            plugin_id=self.plugin_id,
        )
        return self._own(dispose, label=f"command:{name}")

    def register_provider(self, name: str, factory: Any, *, description: str = "") -> Callable[[], Any]:
        dispose = self.llm.add_provider(name, factory, plugin_id=self.plugin_id, description=description)
        return self._own(dispose, label=f"provider:{name}")

    def __repr__(self) -> str:
        return f"<PluginContext id={self.plugin_id!r}>"


__all__ = ["PluginContext"]
