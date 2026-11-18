"""Native agent factory: the one recipe that builds a NativeAgentHost.

Both the plugin API (AgentsPlugin.host) and tests use this module, so the wiring
order for a native agent exists exactly once: resolve the preset revision,
materialize requirement, model, scoped tool registry, preset tool filtering,
native provider, prompt source, budget, checkpoint/trace stores, service bag,
then NativeAgentHost plus the persisted system prompt.

The factory is a plain frozen dataclass of service collaborators (duck-typed,
so tests inject fakes), and build_native_host() is the functional form for
callers that do not want the object. It is wire-free and never imports the v4
agent/session code; the only dependencies are the native loop, the native host,
core.llm, the prompt service protocol and the session schema types.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from deeppyro.agent.budget import BudgetLimits
from deeppyro.agent.checkpoint import CheckpointStore
from deeppyro.agent.events import AgentHooks
from deeppyro.agent.native_host import NativeAgentHost
from deeppyro.api.errors import PluginDependencyError
from deeppyro.api.types import (
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_PTC,
    TEAM_MODE_DISABLED_TOOLS,
    TEAM_TOOL_NAMES,
)
from deeppyro.core.llm import LlmProvider, ToolSpec
from deeppyro.observability.trace import TraceStore
from deeppyro.prompt.prompt import PromptContext, collect_instructions
from deeppyro.session.schema import NativeSession, SessionHeader

__all__ = [
    "AgentBuild",
    "NativeAgentFactory",
    "PresetPromptSource",
    "PromptFacts",
    "PromptSource",
    "build_native_host",
]


class PromptSource(Protocol):
    """Renders the system prompt for one session and one tool surface."""

    def render(self, session: SessionHeader, tools: Sequence[ToolSpec]) -> str: ...


class PromptService(Protocol):
    """The subset of PromptPlugin the factory needs."""

    def build(
        self,
        tools: list[Any],
        prompt_context: Any,
        *,
        scope: str | None = None,
        revision: int | None = None,
        include: Sequence[str] | None = None,
    ) -> str: ...


class PresetResolver(Protocol):
    """The subset of PresetsPlugin the factory needs."""

    def resolve(self, preset_id: str | None = None, *, revision: int | None = None) -> Any: ...

    def is_materialized(self, preset_id: str, revision: int | None = None) -> bool: ...


class NativeProviderResolver(Protocol):
    """The subset of LlmPlugin the factory needs."""

    def native_provider(self, name: str) -> LlmProvider: ...


class ToolRegistryService(Protocol):
    """The subset of ToolsPlugin the factory needs."""

    def registry_for(
        self,
        workspace: str | Path,
        *,
        scope: str | None = None,
        revision: int | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class PromptFacts:
    """Caller-supplied environment facts for one system prompt render.

    The TUI computes these from its permission/approval state; the factory does
    not read them from the v4 runtime. platform defaults to the live platform so
    a test can pin it.
    """

    cwd: Path
    model: str
    permission_preset: str
    approval_policy: str
    platform: str = ""
    sandbox_policy_text: str = ""
    approval_policy_text: str = ""
    env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.cwd, (str, Path)):
            raise ValueError("prompt facts cwd must be a path")
        for name in ("model", "permission_preset", "approval_policy", "platform"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"prompt facts {name} must be a string")
        if not isinstance(self.env, tuple) or any(
            not (isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(p, str) for p in pair))
            for pair in self.env
        ):
            raise ValueError("prompt facts env must be a tuple of (key, value) string pairs")


@dataclass(frozen=True, slots=True)
class PresetPromptSource:
    """Preset-scoped prompt renderer: the only PromptSource implementation.

    context() mirrors the v4 PromptLayer._prompt_context fields so the built-in
    sections render identically; render() is exactly the v4 prompt service call
    with the preset's scope, revision and section allowlist.
    """

    prompt: PromptService
    spec: Any
    facts: Callable[[], PromptFacts]

    def context(self, session: SessionHeader, tools: Sequence[ToolSpec] = ()) -> PromptContext:
        """Build the PromptContext for one session and tool surface."""
        resolved = self.facts()
        preset_instructions = cast(Sequence[str], getattr(self.spec, "instructions", ()) or ())
        instructions = [*collect_instructions(resolved.cwd), *preset_instructions]
        return PromptContext(
            cwd=resolved.cwd,
            platform=resolved.platform or sys.platform,
            session_id=session.id,
            started_at=session.created_at,
            permission_preset=resolved.permission_preset,
            approval_policy=resolved.approval_policy,
            model=resolved.model,
            sandbox_policy_text=resolved.sandbox_policy_text,
            approval_policy_text=resolved.approval_policy_text,
            instructions=list(instructions),
            env=dict(resolved.env),
            direct_tool_names=tuple(tool.name for tool in tools),
        )

    def render(self, session: SessionHeader, tools: Sequence[ToolSpec]) -> str:
        """Render the preset's system prompt for this session and tools."""
        context = self.context(session, tools)
        return self.prompt.build(
            list(tools),
            context,
            scope=getattr(self.spec, "id", None),
            revision=getattr(self.spec, "revision", None),
            include=getattr(self.spec, "prompt_sections", None),
        )


@dataclass(frozen=True, slots=True)
class AgentBuild:
    """Everything the host needs to build one agent in one session.

    The TUI turn driver builds this per session once; services carries the
    plugin services the caller wants visible to tools (the factory adds
    "session" and "tool_registry" itself).
    """

    provider: str
    model: str
    cwd: Path
    preset: str
    preset_revision: int | None = None
    permission: object | None = None
    hooks: AgentHooks | None = None
    prompt_facts: PromptFacts | None = None
    services: Mapping[str, object] = field(default_factory=dict)
    context_window: int = 32_768
    max_steps: int | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.cwd, (str, Path)):
            raise ValueError("agent build cwd must be a path")
        if not isinstance(self.cwd, Path):
            object.__setattr__(self, "cwd", Path(self.cwd))
        if not isinstance(self.provider, str) or not self.provider:
            raise ValueError("agent build provider must be a non-empty string")
        if not isinstance(self.model, str):
            raise ValueError("agent build model must be a string")
        if not isinstance(self.preset, str) or not self.preset:
            raise ValueError("agent build preset must be a non-empty string")
        if type(self.context_window) is not int or self.context_window < 1:
            raise ValueError("context_window must be a positive int")
        for name in ("max_steps",):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError(f"{name} must be a positive int or None")
        for name in ("temperature", "max_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ValueError(f"{name} must be a number or None")


@dataclass(frozen=True, slots=True)
class NativeAgentFactory:
    """The injected build recipe: collaborators in, NativeAgentHost out."""

    prompt: PromptService
    presets: PresetResolver
    llm: NativeProviderResolver
    tools: ToolRegistryService | None = None
    default_model: str = ""
    default_max_steps: int | None = None
    ptc_mode: Callable[[], bool] | None = None

    def build(self, session: NativeSession, build: AgentBuild) -> NativeAgentHost:
        """Build one NativeAgentHost for session from build."""
        return build_native_host(
            session,
            build,
            prompt=self.prompt,
            presets=self.presets,
            llm=self.llm,
            tools=self.tools,
            default_model=self.default_model,
            default_max_steps=self.default_max_steps,
            ptc_mode=self.ptc_mode,
        )


def build_native_host(
    session: NativeSession,
    build: AgentBuild,
    *,
    prompt: PromptService,
    presets: PresetResolver,
    llm: NativeProviderResolver,
    tools: ToolRegistryService | None = None,
    default_model: str = "",
    default_max_steps: int | None = None,
    ptc_mode: Callable[[], bool] | None = None,
) -> NativeAgentHost:
    """Run the native build recipe and return the host.

    Raises PluginDependencyError when the preset revision is not materialized,
    when the preset selects the PTC presentation, when no model can be resolved
    or when the llm service cannot supply a native provider. The system prompt
    is written as the session's last system.message fact when prompt_facts was
    supplied (via NativeAgentHost.attach_prompt).
    """
    if session.closed:
        raise ValueError("session is closed")
    spec = presets.resolve(build.preset, revision=build.preset_revision)
    _require_materialized(presets, spec)
    _refuse_ptc(spec, ptc_mode)
    model = build.model or str(getattr(spec, "model", "") or "") or default_model
    if not model:
        raise PluginDependencyError("no model configured for this native agent build")
    provider = llm.native_provider(build.provider)
    registry = _scoped_registry(session, build, spec, tools)
    max_steps = build.max_steps if build.max_steps is not None else default_max_steps
    service_map: dict[str, Any] = dict(build.services)
    service_map["session"] = session
    service_map["tool_registry"] = registry
    host = NativeAgentHost(
        session=session,
        provider=provider,
        model=model,
        tools=registry,
        cwd=build.cwd,
        hooks=build.hooks,
        budget=BudgetLimits(max_steps=max_steps),
        checkpoint_store=CheckpointStore(),
        trace_store=TraceStore(),
        temperature=build.temperature if build.temperature is None else float(build.temperature),
        max_tokens_per_call=build.max_tokens if build.max_tokens is None else int(build.max_tokens),
        provider_name=build.provider,
        permission=build.permission,
        services=service_map,
    )
    if build.prompt_facts is not None:
        facts = cast(PromptFacts, build.prompt_facts)

        def resolve_facts() -> PromptFacts:
            return facts

        source = PresetPromptSource(prompt=prompt, spec=spec, facts=resolve_facts)
        host.attach_prompt(source, facts)
    return host


def _require_materialized(presets: PresetResolver, spec: Any) -> None:
    if not getattr(spec, "plugins", ()):
        return
    if not presets.is_materialized(spec.id, spec.revision):
        raise PluginDependencyError(
            f"preset {spec.id!r} revision {spec.revision} is not materialized yet; "
            "await presets.ensure() before creating agents for it"
        )


def _refuse_ptc(spec: Any, ptc_mode: Callable[[], bool] | None) -> None:
    presentation = getattr(spec, "presentation", "")
    legacy = ptc_mode is not None and ptc_mode()
    if presentation != AGENT_PRESENTATION_PTC and not legacy:
        return
    raise PluginDependencyError(
        f"preset {getattr(spec, 'id', '')!r} requires the PTC runtime, "
        "which the native agent host does not provide yet"
    )


def _scoped_registry(
    session: NativeSession,
    build: AgentBuild,
    spec: Any,
    tools: ToolRegistryService | None,
) -> Any:
    if tools is None:
        return ()
    registry = tools.registry_for(str(build.cwd), scope=getattr(spec, "id", None), revision=spec.revision)
    return _filter_registry(registry, spec)


def _filter_registry(registry: Any, spec: Any) -> Any:
    """Apply the preset's mode, allowlist and denylist, in that order.

    Same rule as the v4 agents plugin: team presets keep team tools and lose
    subagent tools, default presets do the opposite; a tools allowlist then
    keeps only named tools and tools_exclude removes names last.
    """
    if registry is None:
        return ()
    team_mode = getattr(spec, "mode", "") == AGENT_MODE_TEAM
    disabled: list[str] = list(TEAM_MODE_DISABLED_TOOLS) if team_mode else list(TEAM_TOOL_NAMES)
    allow = getattr(spec, "tools", None)
    names = getattr(registry, "names", None)
    if allow is not None and callable(names):
        allowed = set(allow)
        registered = cast(Iterable[str], names())
        disabled.extend(name for name in registered if name not in allowed)
    disabled.extend(getattr(spec, "tools_exclude", ()) or ())
    without = getattr(registry, "without", None)
    return without(disabled) if callable(without) else registry
