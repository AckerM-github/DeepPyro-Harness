"""Base classes for DeepPyro plugins and services.

API v2 (the current version) has a single injection point: the adapter
constructs the plugin with ``(ctx, config)`` and then calls ``setup()`` with no
arguments::

    class HelloPlugin(api.Plugin):
        plugin_id = "hello"
        inject = (ToolsService, "prompt")  # contracts or plain service names

        def setup(self):
            self.ctx.logger.info("hello, %s", self.config.name)

Plugins written against API v1 declare ``setup(self, ctx, config)``; the adapter
still calls them with both arguments, so existing bundles keep working. A plugin
that wants the strict v2 contract declares ``api_version = 2`` in its own body.

Services are plugins that additionally register a named service:

    class NotesService(api.Service):
        provide = "notes"

        def __init__(self, ctx, config):
            super().__init__(ctx, config)
            self.items = []

The kernel validates the pydantic Config class before construction, so config
here is already a model instance (or a plain dict when Config is None).
"""

from __future__ import annotations

import inspect
import warnings
from collections.abc import Callable, Sequence
from typing import Any, ClassVar, cast

from pydantic import BaseModel

from .context import PluginContext
from .errors import PluginError
from .services import ServiceTypeError, bind_service_type, note_service_value, service_name
from .types import PluginConfig

#: Classes already told about the v1 setup signature (one warning per class).
_WARNED_LEGACY_SETUP: set[type[Any]] = set()


class Plugin:
    """Base class for a host plugin."""

    api_version: ClassVar[int] = 2
    plugin_id: ClassVar[str] = ""
    version: ClassVar[str] = "0.0.0"
    description: ClassVar[str] = ""
    Config: ClassVar[type[BaseModel] | None] = None
    #: Config field names that can be applied to a mounted plugin without a
    #: restart. The kernel service applies them through apply_plugin_config;
    #: settings_write reports applied_live when that succeeds.
    volatile: ClassVar[tuple[str, ...]] = ()
    #: Services this plugin depends on, as contract classes or plain service
    #: names. Contract classes are resolved to their provide name at mount time.
    inject: ClassVar[Sequence[str | type[Any]]] = ()

    #: Set by the adapter before setup runs.
    ctx: PluginContext
    config: PluginConfig

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        self.ctx = ctx
        self.config = config

    def setup(self) -> Any:
        """Register contributions.

        May be synchronous or asynchronous; the kernel awaits the result when
        needed. Registrations made through ctx are undone automatically when
        the plugin fiber unloads.

        Plugins written for API v1 may still declare ``setup(self, ctx,
        config)``; invoke_setup() calls those with both arguments.
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.plugin_id!r}>"


def inject_names(cls: type[Any]) -> list[str]:
    """Resolve a plugin's declared injections to plain service names."""
    names: list[str] = []
    for item in getattr(cls, "inject", ()) or ():
        if isinstance(item, str):
            if item:
                names.append(item)
            continue
        if isinstance(item, type):
            try:
                names.append(service_name(item))
            except ServiceTypeError as exc:
                raise PluginError(f"{_describe(cls)}: inject entry {item!r}: {exc}") from exc
            continue
        raise PluginError(
            f"{_describe(cls)}: inject entries must be service names or contract classes, got {item!r}"
        )
    return names


def invoke_setup(instance: Any, ctx: PluginContext, config: PluginConfig) -> Any:
    """Call a plugin's setup hook with the right argument list.

    API v2 plugins implement ``setup(self)`` and read ctx/config from the
    attributes the constructor stored. Legacy plugins keep receiving the two
    positional arguments they expect, so both styles coexist in one process.
    """
    setup = getattr(instance, "setup", None)
    if not callable(setup):
        return None
    arity = _positional_arity(setup)
    if arity >= 2:
        _warn_legacy_setup(instance)
        return setup(ctx, config)
    if arity == 1:
        return setup(ctx)
    return setup()


def _describe(cls: type[Any]) -> str:
    plugin_id = getattr(cls, "plugin_id", "") or ""
    suffix = f" {plugin_id!r}" if plugin_id else ""
    return f"plugin{suffix} ({cls.__module__}:{cls.__qualname__})"


def _positional_arity(method: Callable[..., Any]) -> int:
    """Count positional parameters a bound setup method expects."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return 2
    arity = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            arity += 1
    return arity


def _warn_legacy_setup(instance: Any) -> None:
    """Warn when a class that opted into v2 still uses the v1 signature."""
    cls = type(instance)
    declared = cls.__dict__.get("api_version")
    if not isinstance(declared, int) or declared < 2:
        return
    if cls in _WARNED_LEGACY_SETUP:
        return
    _WARNED_LEGACY_SETUP.add(cls)
    warnings.warn(
        f"{_describe(cls)} declares api_version {declared} but implements the legacy "
        "setup(ctx, config) signature; implement setup(self) and read self.ctx/self.config",
        DeprecationWarning,
        stacklevel=3,
    )


class Service(Plugin):
    """A plugin that also provides a named service.

    Declaring provide = "name" in a subclass is enough: the class registers
    itself in the typed service table at definition time, so callers can write
    ctx.require(ThatService) without any extra wiring. Constructing the service
    registers the instance itself with the host, and unmounting the plugin
    removes it again. The historical string name (and ctx.get("name")) keeps
    working unchanged.
    """

    provide: ClassVar[str] = ""
    #: The service name this instance is registered under.
    name: str
    #: Intercept config layers applied to this service name by the host
    #: (kernel group rows and preset realms), merged nearest layer first.
    intercept_config: dict[str, Any]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Register the subclass as the typed contract for its provide name."""
        super().__init_subclass__(**kwargs)
        declared = cls.__dict__.get("provide")
        if isinstance(declared, str) and declared:
            bind_service_type(cls, declared)

    def __init__(self, ctx: PluginContext, config: PluginConfig = None) -> None:
        if not self.provide:
            raise PluginError(f"{type(self).__name__}.provide must name a service")
        Plugin.__init__(self, ctx, config)
        self.name = self.provide
        raw = getattr(ctx, "raw", ctx)
        self._raw_ctx = raw
        resolver = getattr(raw, "resolve_intercept", None)
        intercept_config: dict[str, Any] = {}
        if callable(resolver):
            resolved = cast(Callable[[str], Any], resolver)(self.provide)
            if resolved:
                intercept_config = dict(cast("dict[str, Any]", resolved))
        self.intercept_config = intercept_config
        self._provide_disposer: Any = None
        provide = getattr(raw, "provide", None)
        if callable(provide):
            self._provide_disposer = provide(self.provide, self)
            note_service_value(self, self.provide)


__all__ = ["Plugin", "Service", "inject_names", "invoke_setup"]
