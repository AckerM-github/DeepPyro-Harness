"""Base classes for DeepPyro plugins and services (API v3).

API v3 has one mount shape and one initialisation hook: the kernel constructs
the plugin with the context and validated config, then calls `setup(self)` with
no arguments::

    class HelloPlugin(api.Plugin):
        plugin_id = "hello"
        inject = (ToolsPlugin,)          # typed contracts (plain names still allowed)

        def setup(self):
            self.ctx.logger.info("hello, %s", self.config.name)

Children are mounted through the same path:

    self.ctx.mount(ChildPlugin, {"option": 1})

Services are plugins that additionally register a named service::

    class NotesService(api.Service):
        provide = "notes"

        def __init__(self, ctx, config):
            super().__init__(ctx, config)
            self.items = []

The kernel validates the pydantic Config class before construction, so config
here is already a model instance (or a plain dict when Config is None).

API v3 is a hard cut: `api_version` must be 3 (the default) and `setup` takes
only `self`.  Plugins written against the v1/v2 signature
`setup(self, ctx, config)` are rejected at mount time with a
:class:`~deeppyro.api.errors.PluginError` that points at the v3 shape, per ADR
0001 section 7 ("no compatibility shim").  Migration is mechanical: the
constructor already receives the context and config, so move the body of the
old hook into `setup(self)` and read `self.ctx` / `self.config`.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, ClassVar, cast

from pydantic import BaseModel

from .capabilities import CapabilityManifest, RiskLevel, TokenCost, build_manifest
from .context import PluginContext
from .errors import PluginError
from .services import ServiceTypeError, bind_service_type, note_service_value, service_name
from .types import PluginConfig
from .version import API_VERSION


def _positional_arity(method: Any) -> int:
    """Number of positional parameters a bound method declares (0 for setup(self))."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):  # pragma: no cover - builtins and C functions
        return 2
    return sum(
        1
        for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    )


def _describe(cls: type[Any]) -> str:
    """Human readable plugin identity used in errors."""
    plugin_id = str(getattr(cls, "plugin_id", "") or "")
    location = f"{cls.__module__}:{cls.__qualname__}"
    return f"plugin {plugin_id!r} ({location})" if plugin_id else f"plugin ({location})"


class Plugin:
    """Base class for a host plugin (API v3)."""

    api_version: ClassVar[int] = API_VERSION
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
    #: Version constraints for injected services, for example
    #: `{"tools": ">=0.2"}`. Enforced by the kernel capability validation.
    requires_versions: ClassVar[Mapping[str, str]] = {}
    #: Event names this plugin emits or observes (capability manifest only).
    events: ClassVar[tuple[str, ...]] = ()

    # -- capability governance (read by api.capabilities) ------------------
    #: Coarse blast radius of the plugin.
    risk: ClassVar[RiskLevel] = RiskLevel.LOW
    #: Whether mounting/setup can be replayed safely.
    idempotent: ClassVar[bool] = True
    #: Host permissions the plugin needs (for example `tools:write`).
    permissions: ClassVar[tuple[str, ...]] = ()
    #: Sandbox mode the plugin needs, or None when it needs none.
    sandbox: ClassVar[str | None] = None
    #: Optional token/cost metadata for the accounting service.
    token_cost: ClassVar[TokenCost | None] = None
    #: Optional context window assumption, when relevant to the plugin.
    context_window: ClassVar[int | None] = None

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
        """

    def capability_manifest(self) -> CapabilityManifest:
        """Return this plugin's declarative capability manifest.

        Subclasses may override to refine the derived defaults (for example to
        list contributed tools, hooks or token/cost metadata).
        """
        return build_manifest(self)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.plugin_id!r}>"


def resolve_injections(entries: Iterable[str | type[Any]], *, owner: str = "plugin") -> list[str]:
    """Resolve an inject list to plain service names.

    Typed contracts are resolved through :func:`service_name`; plain strings are
    kept for host-defined services and for incremental migration.
    """
    names: list[str] = []
    for item in entries or ():
        if isinstance(item, str):
            if item:
                names.append(item)
            continue
        if isinstance(item, type):
            try:
                names.append(service_name(item))
            except ServiceTypeError as exc:
                raise PluginError(f"{owner}: inject entry {item!r}: {exc}") from exc
            continue
        raise PluginError(f"{owner}: inject entries must be service names or contract classes, got {item!r}")
    return names


def inject_names(cls: type[Any]) -> list[str]:
    """Resolve a plugin's declared injections to plain service names."""
    return resolve_injections(getattr(cls, "inject", ()) or (), owner=_describe(cls))


def invoke_setup(instance: Any, ctx: PluginContext, config: PluginConfig) -> Any:
    """Call a plugin's setup hook under the single API v3 contract.

    Only `setup(self)` is accepted.  API v1/v2 plugins receive an explicit
    error with a migration hint instead of a silent fallback or an implicit
    compatibility branch (ADR 0001 section 7).
    """
    setup = getattr(instance, "setup", None)
    if not callable(setup):
        return None
    declared = int(getattr(type(instance), "api_version", API_VERSION) or API_VERSION)
    if declared != API_VERSION:
        raise PluginError(
            f"{_describe(type(instance))} targets api_version {declared}, but this host only "
            f"speaks API v{API_VERSION}; the v1/v2 plugin API was removed with no compatibility "
            "shim (ADR 0001 section 7) -- declare api_version = "
            f"{API_VERSION} and implement setup(self)"
        )
    if _positional_arity(setup) == 0:
        return setup()
    raise PluginError(
        f"{_describe(type(instance))} implements setup with positional parameters, "
        f"but API v{API_VERSION} has a single mount shape: setup(self). "
        "Read self.ctx and self.config inside setup instead; the v2 setup(ctx, config) "
        "signature was removed with no compatibility shim (ADR 0001 section 7)."
    )


class Service(Plugin):
    """A plugin that also provides a named service.

    Declaring provide = "name" in a subclass is enough: the class registers
    itself in the typed service table at definition time, so callers can write
    ctx.require(ThatService) without any extra wiring. Constructing the service
    registers the instance itself with the host, and unmounting the plugin
    removes it again. Dependent plugins receive it through typed inject.
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
            resolved = resolver(self.provide)
            if resolved:
                intercept_config = dict(cast("dict[str, Any]", resolved))
        self.intercept_config = intercept_config
        self._provide_disposer: Any = None
        provide = getattr(raw, "provide", None)
        if callable(provide):
            self._provide_disposer = provide(self.provide, self)
            note_service_value(self, self.provide)


__all__ = ["Plugin", "Service", "inject_names", "invoke_setup", "resolve_injections"]
