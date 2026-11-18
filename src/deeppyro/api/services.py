"""Typed service lookup over the kernel service container (API v3).

DeepPyro keeps one runtime service container keyed by name: providers register
through ``ctx.provide(name, value)`` and the kernel resolves dependencies by
name. API v3 puts a contract class in front of that container: every service
class that declares ``provide`` is bound to its name automatically, so plugins
ask for the interface they understand without spelling a string:

    class MyPlugin(api.Plugin):
        inject = (HookService,)

        def setup(self):
            hooks = self.ctx.require(HookService)      # raises when missing
            maybe = self.ctx.get(AttachmentStore)      # None when absent

Plugin-facing lookups are typed only: ``PluginContext`` no longer accepts
service-name strings. The name-keyed primitives in this module
(:func:`lookup_service`, :func:`require_service`) stay for internal host code
and adapter-driven mounts that hold a raw kernel context.

Service-name inventory
======================

Providers mounted by the core bundle (36 names) and the class that carries
each one. camelCase names flagged with (!) are the ones a string-only audit
tends to miss; they are ordinary services like the others:

    activity            ActivityService         builtin/activity.py
    agents              AgentsPlugin            builtin/agents.py
    approval            ApprovalService         builtin/sandbox.py
    attachments         AttachmentStore         builtin/attachments.py
    bus                 EventBus                builtin/hooks.py
    commands            CommandsPlugin          builtin/commands.py
    config              KernelConfig            kernel/config_service.py
    goal                GoalService             builtin/goal.py
    hooks               HookService             builtin/hooks.py
    inspect             InspectPlugin           builtin/inspect.py
    jobs                JobsService             builtin/jobs.py
    kernel              KernelRuntime           kernel/runtime.py
    llm                 LlmPlugin               builtin/llm.py
    mcp                 McpPlugin               builtin/mcp.py
    memory              MemoryService           builtin/memory.py
    permissionPresets(!) PermissionPresetsService builtin/sandbox.py
    plugin_manager      PluginManagerPlugin     builtin/plugin_manager.py
    presets             PresetsPlugin           builtin/presets.py
    prompt              PromptPlugin            builtin/prompt_service.py
    ptc                 PtcRuntime              builtin/ptc.py
    retryPolicy(!)      RetryPolicy or dict     llm/retry.py
    sandbox             SandboxPlugin           builtin/sandbox.py
    sandboxPolicy(!)    SandboxPolicyService    builtin/sandbox.py
    schedule            SchedulePlugin          builtin/schedule.py
    sessions            SessionsPlugin          builtin/sessions.py
    settings            SettingsPlugin          builtin/settings.py
    skills              SkillsPlugin            builtin/skills.py
    slots               SlotRegistry            tui/slots.py (TUI host layer)
    subagent            SubagentService         builtin/subagent.py
    team                TeamService             builtin/team.py
    tools               ToolsPlugin             builtin/tools.py
    ui                  UiService               tui/ui.py (TUI host layer)
    user_questions      UserQuestionsService    builtin/user_questions.py
    working_directory   WorkingDirectoryService builtin/working_directory.py

The native runtime mounts no implicit services: everything in this inventory is
provided by a plugin, including the kernel's own `config` and `kernel` services.

Name resolution
===============

service_name(contract) resolves a contract class in this order:

1. an explicit bind_service_type(contract, name) registration;
2. a value registered through PluginContext.provide: its class maps to the name
   it was provided under (the first registration wins);
3. the class attribute convention already used by plugins and domain services:
   provide = "hooks". A non-string provide (multi-service plugins) or an empty
   one means "cannot resolve" and raises ServiceTypeError.

Contract types that predate the convention (EventBus, HookService) are bound
explicitly at the bottom of this module; RetryPolicy binds itself in
deeppyro.llm.retry so importing the API never pulls the LLM stack.

Failure semantics
=================

* ctx.get(contract) / App.get(contract) return None for both an absent service
  and a contract that has no known name (optional lookups stay cheap and
  typo-tolerant).
* ctx.require(contract) / App.require(contract) raise ServiceNotFoundError (a
  PluginDependencyError, therefore a LookupError) when the name is known but no
  active instance exists, and ServiceTypeError when the class cannot be
  resolved at all.
* service-name strings are not part of the plugin API: PluginContext
  get/require/has take contract classes only (API v3 hard cut). App has a
  host-level service lookup by name (and a string form of App.get) for embedders
  and tests that address services by name; neither is available to plugins.

Typed lookups from inside a service
===================================

A service instance built by the kernel adapter holds a PluginContext in
self.ctx; adapter-driven mounts (presets, test doubles) may hold a raw
RuntimeContext instead. Service methods therefore resolve typed services
through require_service(self.ctx, Contract) / lookup_service(self.ctx,
Contract); the helpers normalise the contract to its name first and then read
the container, so isolate semantics stay identical for both context shapes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, TypeVar, cast

from deeppyro.core.bus import EventBus
from deeppyro.hooks.service import HookService

from .errors import PluginDependencyError, PluginError

T = TypeVar("T")

__all__ = [
    "ServiceBag",
    "ServiceNotFoundError",
    "ServiceTypeError",
    "SupportsServiceLookup",
    "bind_service_type",
    "contract_path",
    "lookup_service",
    "note_service_value",
    "require_service",
    "service_name",
]

#: Explicit contract -> name bindings (highest priority).
_EXPLICIT: dict[type[Any], str] = {}
#: Types learned from a live provider value; setdefault keeps the first one so
#: plugin reloads never fight over the same class.
_DISCOVERED: dict[type[Any], str] = {}


class ServiceTypeError(PluginError):
    """A class cannot be used as a typed service contract.

    Raised for a non-class argument, a class without any declared name, or a
    non-string provide attribute (multi-service plugins bind the contract
    explicitly).
    """


class ServiceNotFoundError(PluginDependencyError):
    """The contract resolved to a name but the context holds no active service."""

    def __init__(self, message: str, *, service: str = "", contract: type[Any] | None = None) -> None:
        super().__init__(message)
        self.service = service
        self.contract = contract


def contract_path(contract: type[Any]) -> str:
    """Return a readable module-qualified path for an error message."""
    return f"{contract.__module__}.{contract.__qualname__}"


def bind_service_type(contract: type[Any], name: str) -> None:
    """Bind contract to the canonical service name.

    Use this for contract types that do not declare provide (or must map to a
    different name than their own class attribute). Explicit bindings win over
    names discovered from provider values.
    """
    if not isinstance(contract, type):
        raise TypeError(f"service contract must be a class, got {contract!r}")
    if not isinstance(name, str) or not name:
        raise ValueError(f"service name must be a non-empty string, got {name!r}")
    _EXPLICIT[contract] = name


def note_service_value(value: Any, name: str) -> None:
    """Remember that value comes from the service registered under name.

    Called by deeppyro.api.PluginContext.provide for every provided value, so
    typed lookups work without touching the provider.
    """
    if value is None or not isinstance(name, str) or not name:
        return
    _DISCOVERED.setdefault(type(value), name)


def service_name(contract: type[Any]) -> str:
    """Resolve the service name for a contract class.

    Raises:
        ServiceTypeError: when the class declares no usable name; bind it with
            bind_service_type first.
    """
    if not isinstance(contract, type):
        raise ServiceTypeError(f"service contracts must be classes, got {contract!r}")
    explicit = _EXPLICIT.get(contract)
    if explicit is not None:
        return explicit
    discovered = _DISCOVERED.get(contract)
    if discovered is not None:
        return discovered
    declared = getattr(contract, "provide", None)
    if isinstance(declared, str) and declared:
        _DISCOVERED.setdefault(contract, declared)
        return declared
    path = contract_path(contract)
    if declared is not None:
        raise ServiceTypeError(
            f"{path} declares a non-string provide ({declared!r}); "
            f"call bind_service_type({contract.__name__}, '<service-name>') before a typed lookup"
        )
    raise ServiceTypeError(
        f"{path} is not bound to a service name; "
        f"call bind_service_type({contract.__name__}, '<service-name>') or register the provider first"
    )


class SupportsServiceLookup(Protocol):
    """Any context that can resolve a service by its canonical name.

    Both PluginContext and the kernel RuntimeContext satisfy this shape: the
    kernel cannot be imported here (api sits below kernel), so the lookup
    helpers only depend on the one method they use.
    """

    def get(self, name: str, /) -> Any | None:
        """Return the service registered under name, or None."""
        ...


def _name_or_none(service: str | type[Any]) -> str | None:
    """Resolve a service argument to its name, or None when it cannot."""
    if isinstance(service, str):
        return service or None
    try:
        return service_name(service)
    except ServiceTypeError:
        return None


def lookup_service(ctx: object, service: str | type[Any]) -> Any | None:
    """Resolve one service through a raw runtime context or a PluginContext.

    The canonical name is resolved first; a raw RuntimeContext looks it up as
    the named primitive it is, while a PluginContext (typed-only in API v3) is
    unwrapped to its raw runtime context so the same code works in both places.
    """
    name = _name_or_none(service)
    if name is None:
        return None
    raw = getattr(ctx, "raw", None)
    target = raw if raw is not None else ctx
    getter = getattr(target, "get", None)
    if not callable(getter):
        return None
    return cast("Any | None", getter(name))


def require_service(ctx: object, service: str | type[Any], *, owner: str = "plugin") -> Any:
    """Like lookup_service, but raise when the service is not available.

    String arguments keep the historical PluginDependencyError wording; class
    arguments raise ServiceNotFoundError with the contract path in the message.
    """
    name = service if isinstance(service, str) else service_name(service)
    value = lookup_service(ctx, name)
    if value is not None:
        return value
    if isinstance(service, str):
        raise PluginDependencyError(f"plugin {owner!r} requires service {name!r}, which is not available")
    raise ServiceNotFoundError(
        f"plugin {owner!r} requires service {name!r} ({contract_path(service)}), which is not available",
        service=name,
        contract=service,
    )


class ServiceBag:
    """Typed view over a legacy dict[str, Any] service bag.

    Tool contexts and hosts still carry a plain dict of services; the native
    agent host wraps it once and asks for contract classes through this view
    while keeping the dict itself intact for the tools pipeline.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = values

    def get(self, service: type[T]) -> T | None:
        """Return the bag value for a contract, or None when absent/unknown."""
        try:
            name = service_name(service)
        except ServiceTypeError:
            return None
        return cast(T, self._values.get(name))

    def require(self, service: type[T]) -> T:
        """Return the bag value for a contract, or raise ServiceNotFoundError."""
        name = service_name(service)
        value = self._values.get(name)
        if value is None:
            raise ServiceNotFoundError(
                f"requires service {name!r} ({contract_path(service)}), which is not in this service bag",
                service=name,
                contract=service,
            )
        return cast(T, value)


# Contract types that predate the provide convention and declare no name.
bind_service_type(EventBus, "bus")
bind_service_type(HookService, "hooks")
