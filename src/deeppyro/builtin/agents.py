"""The agents service: builds AgentSession instances."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.api import (
    AGENT_MODE_DEFAULT,
    AGENT_MODE_TEAM,
    AGENT_PRESENTATION_NATIVE,
    AGENT_PRESENTATION_PTC,
    TEAM_MODE_DISABLED_TOOLS,
    TEAM_TOOL_NAMES,
    PluginDependencyError,
    Service,
)


class AgentsPlugin(Service):
    """Creates agent sessions wired to the prompt and presets services."""

    provide = "agents"
    inject: ClassVar[list[str]] = ["config", "prompt", "presets"]
    plugin_id = "agents"
    version = "0.1.0"
    description = "agent session factory"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self._ctx = ctx
        cfg = ctx.service("config").value
        self._agent_config = cfg.agent
        self._prompt_service = ctx.service("prompt")
        self._presets = ctx.service("presets")
        # Optional: only needed when the caller lets the service build the
        # preset-scoped tool registry itself.
        self._tools = ctx.get("tools") if hasattr(ctx, "get") else None

    def options(self, **overrides: Any) -> AgentOptions:
        values: dict[str, Any] = {
            "max_steps": self._agent_config.maxSteps,
            "max_tokens": self._agent_config.maxTokens,
            "temperature": self._agent_config.temperature,
        }
        values.update(overrides)
        return AgentOptions(**values)

    def preset(self, preset_id: str | None = None, *, revision: int | None = None) -> Any:
        """Resolve one preset spec (the default when preset_id is None)."""
        return self._presets.resolve(preset_id, revision=revision)

    def create(
        self,
        *,
        session: Any,
        provider: Any,
        registry: Any | None = None,
        options: AgentOptions,
        sink: Any,
        permission: Any | None = None,
        cwd: str,
        services: dict[str, Any] | None = None,
        preset: str | None = None,
        preset_revision: int | None = None,
    ) -> AgentSession:
        """Create an agent pinned to the latest (or a specific) preset revision.

        The resolved revision is snapshotted here: the agent keeps its tools,
        prompt sections and options for life, even when the preset is edited
        afterwards. Hosts that let the service build the registry (registry
        omitted) must have awaited presets.ensure() for revisions that carry
        plugin rows.
        """
        spec = self._presets.resolve(preset, revision=preset_revision)
        self._require_materialized(spec)
        if spec.model and not options.model:
            options = replace(options, model=spec.model)
        if spec.instructions:
            options = replace(options, instructions=[*options.instructions, *spec.instructions])
        if registry is None:
            registry = self._registry_for(cwd, spec)
        registry = self._filter_registry(registry, spec)

        def build_prompt(tools: list[Any], prompt_context: Any) -> str:
            return self._prompt_service.build(
                tools,
                prompt_context,
                scope=spec.id,
                revision=spec.revision,
                include=spec.prompt_sections,
            )

        surface = self._surface_registry(registry, spec)
        agent = AgentSession(
            session=session,
            provider=provider,
            registry=registry,
            options=options,
            sink=sink,
            permission=permission,
            cwd=cwd,
            services=services,
            prompt_builder=build_prompt,
            surface_registry=surface,
            runtime_context=spec.runtime_context,
        )
        # Kept for tests and hosts that inspect what the model was offered.
        agent.surface_registry = surface
        #: The tool presentation the model actually sees (TUI status line).
        agent.presentation = AGENT_PRESENTATION_PTC if surface is not None else AGENT_PRESENTATION_NATIVE
        agent.preset = spec.id
        agent.preset_revision = spec.revision
        agent.preset_spec = spec
        return agent

    def _require_materialized(self, spec: Any) -> None:
        if not getattr(spec, "plugins", ()):
            return
        is_materialized = getattr(self._presets, "is_materialized", None)
        if callable(is_materialized) and not is_materialized(spec.id, spec.revision):
            raise PluginDependencyError(
                f"preset {spec.id!r} revision {spec.revision} is not materialized yet; "
                "await presets.ensure() before creating agents for it"
            )

    def _registry_for(self, cwd: Any, spec: Any) -> Any:
        if self._tools is None:
            raise PluginDependencyError(
                "the agents service needs the tools service to build a preset registry; "
                "pass registry= explicitly"
            )
        return self._tools.registry_for(str(cwd), scope=spec.id, revision=spec.revision)

    def _surface_registry(self, registry: Any, spec: Any) -> Any:
        """Return the model-facing registry for this agent, or None for all tools.

        A preset's own presentation decides first: 'ptc' presents exactly one
        direct tool (run_code) and every other tool is reached from inside the
        program, while 'native' keeps every tool direct.  The legacy host-wide
        [ptc].mode = "ptc" switch still narrows default-mode presets so older
        compositions keep their behaviour.  The full registry always backs the
        nested dispatcher, the system prompt's tools:sdk section and this
        service, so only the request surface is narrowed here.  Team presets
        keep the native surface: they orchestrate members through team_* tools
        that the model itself has to call.
        """
        if registry is None:
            return None
        if getattr(spec, "mode", AGENT_MODE_DEFAULT) != AGENT_MODE_DEFAULT:
            return None
        presentation = getattr(spec, "presentation", AGENT_PRESENTATION_NATIVE)
        if presentation == AGENT_PRESENTATION_PTC:
            surface = self._ptc_surface(registry)
            if surface is None:
                raise PluginDependencyError(
                    f"preset {getattr(spec, 'id', '')!r} selects the ptc presentation, "
                    "but run_code is not registered in its tool registry"
                )
            return surface
        if self._legacy_ptc_mode():
            return self._ptc_surface(registry)
        return None

    def _legacy_ptc_mode(self) -> bool:
        """Whether the host-wide PTC switch is on (deprecated preset coupling)."""
        ptc = self._ctx.get("ptc") if hasattr(self._ctx, "get") else None
        return ptc is not None and getattr(ptc, "mode", "native") == "ptc"

    @staticmethod
    def _ptc_surface(registry: Any) -> Any:
        """Narrow a registry to run_code, or None when it cannot be narrowed."""
        names = getattr(registry, "names", None)
        without = getattr(registry, "without", None)
        if not callable(names) or not callable(without):
            return None
        registered = list(names())
        if "run_code" not in registered:
            return None
        return without([name for name in registered if name != "run_code"])

    def _filter_registry(self, registry: Any, spec: Any) -> Any:
        """Apply the preset's mode, allowlist and denylist to a tool registry.

        The built-in modes partition the delegation tools: a default-mode agent
        delegates one-shot through `subagent`, while team mode orchestrates
        persistent members through `team_*` and never gets the one-shot tool.
        Team tools are scoped to the team preset already; filtering here keeps
        the split true even for an explicitly supplied registry.

        On top of the mode filter, PresetSpec.tools keeps only the allowlisted
        names (None means every tool) and PresetSpec.tools_exclude removes names
        last, so a preset can both start from a small surface and drop tools the
        host or a plugin adds globally.
        """
        if registry is None:
            return None
        if getattr(spec, "mode", "") == AGENT_MODE_TEAM:
            disabled: list[str] = list(TEAM_MODE_DISABLED_TOOLS)
        else:
            disabled = list(TEAM_TOOL_NAMES)
        allow = getattr(spec, "tools", None)
        names = getattr(registry, "names", None)
        if allow is not None and callable(names):
            allowed = set(allow)
            disabled.extend(name for name in names() if name not in allowed)
        disabled.extend(getattr(spec, "tools_exclude", ()) or ())
        without = getattr(registry, "without", None)
        if callable(without):
            return without(disabled)
        return registry
