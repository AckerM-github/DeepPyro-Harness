"""The context object handed to every plugin."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from deeppyro.core.events import is_bus_event

from .errors import PluginDependencyError


class _NoopUi:
    """Stand-in returned by :attr:`PluginContext.ui` when no ui service exists.

    Headless sessions and tests should not have to special-case the service:
    every producer method returns ``False`` and the counters stay at zero.
    """

    available = False

    def post(self, action: Any, /, **payload: Any) -> bool:
        return False

    def notify(self, text: str, level: str = "info") -> bool:
        return False

    def open_screen(self, slot_id: str | None = None) -> bool:
        return False

    def open_panel(self, slot_id: str | None = None) -> bool:
        return False

    def set_status(self, slot_id: str, text: str) -> bool:
        return False

    def add_row(self, text: str, *, level: str = "info") -> bool:
        return False

    def refresh(self) -> bool:
        return False

    @property
    def pending(self) -> int:
        return 0

    @property
    def error_messages(self) -> tuple[str, ...]:
        return ()

    def stats(self) -> dict[str, Any]:
        return {"bound": False, "queued": 0, "delivered": 0, "dropped": 0, "errors": 0}

    def __repr__(self) -> str:
        return "<NoopUi>"


_NOOP_UI = _NoopUi()


def _bus_options(options: Any) -> dict[str, Any]:
    """Translate cordis listener options into kernel-bus keyword arguments."""
    if options is None:
        return {}
    if isinstance(options, Mapping):
        data = dict(options)
    else:  # pragma: no cover - cordis passes mappings, this keeps objects usable
        data = {key: getattr(options, key) for key in ("prepend", "once", "scope") if hasattr(options, key)}
    mapped: dict[str, Any] = {}
    if data.get("prepend"):
        mapped["prepend"] = True
    if data.get("once"):
        mapped["once"] = True
    if data.get("global") or data.get("global_"):
        mapped["global_"] = True
    scope = data.get("scope")
    if scope is not None:
        mapped["scope"] = scope
    return mapped


class PluginContext:
    """A thin, plugin-friendly facade over the raw Cordis context.

    Attributes not defined here are looked up on the raw context, so plugin
    authors can still use events, fibers and the runtime directly. The
    registration helpers add one important behaviour: whatever they register
    is torn down when the plugin fiber unloads.
    """

    __slots__ = (
        "_logger",
        "_raw",
        "config",
        "default_revision",
        "default_scope",
        "plugin_id",
    )

    def __init__(
        self,
        raw: Any,
        *,
        plugin_id: str = "",
        config: Any = None,
        default_scope: str | None = None,
        default_revision: int | None = None,
    ) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "config", config)
        object.__setattr__(self, "default_scope", default_scope)
        object.__setattr__(self, "default_revision", default_revision)
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
    def plugin_dir(self) -> Path | None:
        """Directory of this plugin's package, when it was installed as one.

        Plugin packages live at ``<plugins_dir>/<id>/`` and carry a
        ``plugin.toml``. Returning ``None`` for in-tree plugins keeps callers
        free to treat the value as purely optional.
        """
        if not self.plugin_id:
            return None
        from deeppyro.paths import plugins_dir

        candidate = plugins_dir() / self.plugin_id
        if (candidate / "plugin.toml").is_file():
            return candidate
        return None

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

    @property
    def ui(self) -> Any:
        """Thread-safe UI action channel, or a no-op stand-in in headless runs.

        Plugins may call this from any thread; actions are drained by the TUI
        on its own thread. Without a TUI every method simply returns ``False``.
        """
        service = self.get("ui")
        return service if service is not None else _NOOP_UI

    # -- lifecycle helpers ------------------------------------------------
    def effect(self, execute: Callable[[], Any], label: str = "anonymous") -> Any:
        return self._raw.effect(execute, label=f"{self.plugin_id}:{label}")

    def on(self, event: Any, handler: Any, options: Any = None) -> Any:
        """Subscribe to an event and return an idempotent disposer.

        Domain events (anything in ``deeppyro.core.events.EVENT_SPECS``) live on
        the kernel bus, exactly like DSH, so a plugin may subscribe with
        ``ctx.on("tools/pre-execute", ...)`` and observe the real dispatch. The
        handler signature follows the event mode: ``handler(payload)`` for emit
        events, ``handler(payload, next_step)`` for waterfall events. Any other
        name keeps the cordis behaviour the kernel itself uses.
        """
        bus = self._bus()
        if bus is not None and isinstance(event, str) and is_bus_event(event):
            return bus.on(event, handler, **_bus_options(options))
        return self._raw.on(event, handler, options)

    def once(self, event: Any, handler: Any, options: Any = None) -> Any:
        """Subscribe for a single dispatch (see :meth:`on`)."""
        bus = self._bus()
        if bus is not None and isinstance(event, str) and is_bus_event(event):
            return bus.on(event, handler, once=True, **_bus_options(options))
        return self._raw.once(event, handler, options)

    def _bus(self) -> Any | None:
        """The kernel event bus, when this context can reach it."""
        bus = self.get("hooks")
        if bus is None or not callable(getattr(bus, "on", None)):
            return None
        return bus

    def emit(self, *args: Any) -> Any:
        return self._raw.emit(*args)

    def plugin(self, plugin: Any, config: Any = None) -> Any:
        return self._raw.plugin(plugin, config)

    def inject(self, inject: Any, callback: Any) -> Any:
        return self._raw.inject(inject, callback)

    def isolate(self, name: str, label: Any = None) -> PluginContext:
        raw = self._raw.isolate(name, label)
        return self._child(raw)

    def intercept(self, name: str, config: Any) -> PluginContext:
        raw = self._raw.intercept(name, config)
        return self._child(raw)

    def _child(self, raw: Any) -> PluginContext:
        """Wrap a derived raw context, keeping the registration defaults."""
        return PluginContext(
            raw,
            plugin_id=self.plugin_id,
            config=self.config,
            default_scope=self.default_scope,
            default_revision=self.default_revision,
        )

    # -- typed registration ----------------------------------------------
    def _own(self, dispose: Callable[[], Any], label: str) -> Callable[[], Any]:
        self.effect(lambda: dispose, label=label)
        return dispose

    def contribution_scope(self, scope: str | None) -> tuple[str | None, int | None]:
        """Resolve the (scope, revision) tag for a contribution from this context.

        Inside a preset realm the host passes default_scope/default_revision so
        contributions are pinned to the revision that was materialized and an
        unscoped registration cannot leak into other presets. Contributions that
        explicitly target another scope are tagged with no revision.
        """
        if scope is None:
            return self.default_scope, self.default_revision
        if scope == self.default_scope:
            return scope, self.default_revision
        return scope, None

    def register_tool(self, factory: Any, *, scope: str | None = None) -> Callable[[], Any]:
        resolved_scope, revision = self.contribution_scope(scope)
        dispose = self.tools.add(
            factory,
            scope=resolved_scope,
            revision=revision,
            plugin_id=self.plugin_id,
        )
        return self._own(dispose, label="tool")

    def register_prompt_section(
        self, name: str, order: int, renderer: Any, *, scope: str | None = None
    ) -> Callable[[], Any]:
        resolved_scope, revision = self.contribution_scope(scope)
        dispose = self.prompt.add_section(
            name,
            order,
            renderer,
            plugin_id=self.plugin_id,
            scope=resolved_scope,
            revision=revision,
        )
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
