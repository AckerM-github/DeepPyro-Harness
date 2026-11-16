"""Typed service lookup over the cordis service container (roadmap step 3).

DeepPyro keeps cordis as the runtime service container: providers still call
ctx.provide(name, value) and consumers may still call ctx.get(name) or
ctx.service(name), so external plugins keep working. On top of that container
this module adds a type key for the same services: every provider value
registers its own class, so a consumer can ask for the interface it understands
without spelling a string:

    class MyPlugin(api.Plugin):
        inject = ["hooks"]

        def setup(self, ctx, config):
            hooks = ctx.require(HookService)      # raises when missing
            maybe = ctx.get(AttachmentStore)      # None when absent

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
    projections         ProjectionsService      builtin/projections.py
    prompt              PromptPlugin            builtin/prompt_service.py
    ptc                 PtcRuntime              builtin/ptc.py
    retryPolicy(!)      RetryPolicy or dict     builtin/llm_retry.py
    sandbox             SandboxPlugin           builtin/sandbox.py
    sandboxPolicy(!)    SandboxPolicyService    builtin/sandbox.py
    schedule            SchedulePlugin          builtin/schedule.py
    session_query       SessionQueryPlugin      builtin/session_query.py
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

The vendored cordis context additionally mounts events (EventsService), logger
(LoggerService), reflect (ReflectService) and registry (RegistryService); those
stay outside the typed inventory because their contract types live in the
immutable vendor tree.

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
* string lookups keep their historical behaviour: ctx.get("hooks") returns None
  or the value, ctx.service("hooks") raises PluginDependencyError, and
  App.service("hooks") raises LookupError.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from deeppyro.core.bus import EventBus
from deeppyro.hooks.service import HookService

from .errors import PluginDependencyError, PluginError

T = TypeVar("T")

__all__ = [
    "ServiceBag",
    "ServiceNotFoundError",
    "ServiceTypeError",
    "bind_service_type",
    "contract_path",
    "note_service_value",
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


class ServiceBag:
    """Typed view over a legacy dict[str, Any] service bag.

    Tool contexts and hosts still carry a plain dict of services;
    AgentSession wraps it once and asks for contract classes through this view
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
