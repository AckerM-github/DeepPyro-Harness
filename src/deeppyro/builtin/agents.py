"""The agents service: builds AgentSession instances."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.api import PluginDependencyError, Service


class AgentsPlugin(Service):
    """Creates agent sessions wired to the prompt and presets services."""

    provide = "agents"
    inject: ClassVar[list[str]] = ["config", "prompt", "presets"]
    plugin_id = "agents"
    version = "0.1.0"
    description = "agent session factory"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
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

        def build_prompt(tools: list[Any], prompt_context: Any) -> str:
            return self._prompt_service.build(
                tools,
                prompt_context,
                scope=spec.id,
                revision=spec.revision,
            )

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
        )
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
