"""The agents service: builds AgentSession instances."""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.api import Service


class AgentsPlugin(Service):
    """Creates agent sessions wired to the prompt service."""

    provide = "agents"
    inject: ClassVar[list[str]] = ["config"]
    plugin_id = "agents"
    version = "0.1.0"
    description = "agent session factory"

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        cfg = ctx.service("config").value
        self._agent_config = cfg.agent
        prompt = ctx.get("prompt")
        self._prompt_builder = getattr(prompt, "build", None)

    def options(self, **overrides: Any) -> AgentOptions:
        values: dict[str, Any] = {
            "max_steps": self._agent_config.maxSteps,
            "max_tokens": self._agent_config.maxTokens,
            "temperature": self._agent_config.temperature,
        }
        values.update(overrides)
        return AgentOptions(**values)

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
    ) -> AgentSession:
        return AgentSession(
            session=session,
            provider=provider,
            registry=registry,
            options=options,
            sink=sink,
            permission=permission,
            cwd=cwd,
            services=services,
            prompt_builder=self._prompt_builder,
        )
