"""Cordis plugins that wire the domain modules into kernel services.

This module is the **only** place where the kernel imports the domain packages
(and it does so lazily, inside constructors/methods), so the two layers stay
honest:

* domain modules (session/llm/tools/sandbox/agent/prompt/compact/cli) hold pure
  logic and never import cordis;
* the kernel owns the service graph and boot order;
* the CLI resolves services from the booted app instead of assembling modules
  by hand.

Service names are stable API:

======== ==============================================================
config    KernelConfig (registered by `create_app`, value: Config)
frontend  FrontendPlugin.frontend -> cli.frontend.TerminalFrontend
llm       LlmPlugin.registry -> llm.LlmRegistry; .provider(name)
sessions  SessionPlugin.store -> session.SessionStore; .create/.open/.list
sandbox   SandboxPlugin.gate(workspace, ask) -> sandbox.PermissionGate
tools     ToolsPlugin.registry_for(workspace) -> tools.ToolRegistry
agents    AgentPlugin.create(...) -> agent.AgentSession
======== ==============================================================
"""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro._vendor.cordis_port import Service


class FrontendPlugin(Service):
    """Provides the single terminal presentation service (name: `frontend`)."""

    provide = "frontend"

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)
        from deeppyro.cli.frontend import TerminalFrontend

        self.frontend = TerminalFrontend()


class LlmPlugin(Service):
    """Provides the LLM provider registry (name: `llm`)."""

    provide = "llm"
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)
        from deeppyro.llm import LlmRegistry

        self.registry = LlmRegistry(self.ctx.config.value)

    def provider(self, name: str) -> Any:
        """Resolve one provider by name (raises ConfigError/KeyError)."""
        return self.registry.get(name)


class SessionPlugin(Service):
    """Provides the durable session store (name: `sessions`)."""

    provide = "sessions"
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)
        from deeppyro.config import session_dir
        from deeppyro.session import SessionStore

        self.store = SessionStore(root=session_dir(self.ctx.config.value))

    def create(self, cwd: Any, **kwargs: Any) -> Any:
        return self.store.create(cwd, **kwargs)

    def open(self, session_id: str) -> Any:
        return self.store.open(session_id)

    def list(self) -> list[Any]:
        return self.store.list()


class SandboxPlugin(Service):
    """Provides sandbox gate factories (name: `sandbox`)."""

    provide = "sandbox"
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)

    def gate(self, workspace: Any, ask: Any = None) -> Any:
        """Build a PermissionGate for one workspace (ask: async approval cb)."""
        from deeppyro.sandbox import PermissionGate

        agent = self.ctx.config.value.agent
        return PermissionGate(
            agent.permissionPreset,
            agent.approvalPolicy,
            workspace,
            ask=ask,
        )


class ToolsPlugin(Service):
    """Provides tool-registry factories (name: `tools`)."""

    provide = "tools"
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)

    def registry_for(self, workspace: Any) -> Any:
        """Build the built-in ToolRegistry bound to `workspace`."""
        from deeppyro.tools import ToolRegistry, builtin_tools

        return ToolRegistry(tools=builtin_tools(self.ctx.config.value, workspace))


class AgentPlugin(Service):
    """Provides AgentSession factories (name: `agents`)."""

    provide = "agents"
    inject: ClassVar[list[str]] = ["config"]

    def __init__(self, ctx: Any, config: dict[str, Any] | None = None) -> None:
        super().__init__(ctx)

    def create(
        self,
        *,
        session: Any,
        provider: Any,
        registry: Any,
        options: Any,
        sink: Any,
        permission: Any,
        cwd: Any,
    ) -> Any:
        """Build an AgentSession from already-resolved collaborators."""
        from deeppyro.agent import AgentSession

        return AgentSession(
            session=session,
            provider=provider,
            registry=registry,
            options=options,
            sink=sink,
            permission=permission,
            cwd=cwd,
        )

    def options(self, **overrides: Any) -> Any:
        """Build AgentOptions from config + per-command overrides."""
        from deeppyro.agent import AgentOptions

        agent_cfg = self.ctx.config.value.agent
        values: dict[str, Any] = {
            "max_steps": agent_cfg.maxSteps,
            "max_tokens": agent_cfg.maxTokens,
            "temperature": agent_cfg.temperature,
        }
        values.update(overrides)
        return AgentOptions(**values)
