"""The agents service: builds AgentSession instances."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.api import Service


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

    def options(self, **overrides: Any) -> AgentOptions:
        values: dict[str, Any] = {
            "max_steps": self._agent_config.maxSteps,
            "max_tokens": self._agent_config.maxTokens,
            "temperature": self._agent_config.temperature,
        }
        values.update(overrides)
        return AgentOptions(**values)

    def preset(self, preset_id: str | None = None) -> Any:
        """Resolve one preset spec (the default when preset_id is None)."""
        return self._presets.resolve(preset_id)

    def create(
        self,
        *,
        session: Any,
        provider: Any,
        registry: Any,
        options: AgentOptions,
        sink: Any,
        permission: Any | None = None,
        cwd: str,
        services: dict[str, Any] | None = None,
        preset: str | None = None,
    ) -> AgentSession:
        spec = self._presets.resolve(preset)
        if spec.model and not options.model:
            options = replace(options, model=spec.model)
        if spec.instructions:
            options = replace(options, instructions=[*options.instructions, *spec.instructions])

        def build_prompt(tools: list[Any], prompt_context: Any) -> str:
            return self._prompt_service.build(tools, prompt_context, scope=spec.id)

        return AgentSession(
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
