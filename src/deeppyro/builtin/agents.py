"""The agents service: builds native agent hosts."""

from __future__ import annotations

import contextlib
import logging
import weakref
from collections.abc import Callable
from typing import Any, ClassVar

from deeppyro.agent.native_factory import AgentBuild, NativeAgentFactory
from deeppyro.agent.native_host import NativeAgentHost
from deeppyro.api import (
    PluginConfig,
    PluginContext,
    PluginDependencyError,
    Service,
)
from deeppyro.api.services import lookup_service
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.presets import PresetsPlugin
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.builtin.tools import ToolsPlugin
from deeppyro.kernel.config_service import KernelConfig
from deeppyro.ptc.runtime import PtcRuntime
from deeppyro.session.schema import NativeSession

logger = logging.getLogger(__name__)


class AgentsPlugin(Service):
    """Creates agent sessions wired to the prompt and presets services."""

    provide = "agents"
    inject: ClassVar[list[str]] = ["config", "prompt", "presets"]
    plugin_id = "agents"
    version = "0.3.0"
    description = "agent session factory"

    def __init__(self, ctx: PluginContext, config: PluginConfig) -> None:
        super().__init__(ctx, config)
        self._ctx = ctx
        cfg = ctx.require(KernelConfig).value
        self._agent_config = cfg.agent
        self._prompt_service = ctx.require(PromptPlugin)
        self._presets = ctx.require(PresetsPlugin)
        # Optional: only needed when the caller lets the service build the
        # preset-scoped tool registry itself.
        self._tools = ctx.get(ToolsPlugin) if hasattr(ctx, "get") else None
        #: Every agent this service built that the garbage collector has not
        #: reclaimed yet.  There is no disposal hook on NativeAgentHost, so
        #: hosts that need "is it still the live instance" read this set.
        self._agents: weakref.WeakSet[Any] = weakref.WeakSet()
        self._created: list[Callable[[Any], None]] = []

    def preset(self, preset_id: str | None = None, *, revision: int | None = None) -> Any:
        """Resolve one preset spec (the default when preset_id is None)."""
        return self._presets.resolve(preset_id, revision=revision)

    def _registry(self) -> weakref.WeakSet[Any]:
        """The live-agent set, created on first use for uninitialised plugins."""
        registry = getattr(self, "_agents", None)
        if registry is None:
            registry = weakref.WeakSet()
            self._agents = registry
        return registry

    def _observers(self) -> list[Callable[[Any], None]]:
        """The creation observers, created on first use."""
        observers = getattr(self, "_created", None)
        if observers is None:
            observers = []
            self._created = observers
        return observers

    def live_agents(self) -> tuple[Any, ...]:
        """Agents this service created that are still reachable."""
        return tuple(self._registry())

    def is_live(self, agent: Any) -> bool:
        """Whether the agent is one this service created and has not closed."""
        if agent is None or agent not in self._registry():
            return False
        return not getattr(agent, "closed", False)

    def on_created(self, callback: Callable[[Any], None]) -> Callable[[], None]:
        """Observe every agent created from now on; returns the disposer."""
        observers = self._observers()
        observers.append(callback)

        def dispose() -> None:
            with contextlib.suppress(ValueError):
                observers.remove(callback)

        return dispose

    def _register(self, agent: Any) -> None:
        """Remember one agent and tell the creation observers about it."""
        self._registry().add(agent)
        for callback in tuple(self._observers()):
            try:
                callback(agent)
            except Exception:
                logger.exception("agents: a creation observer failed")

    # ------------------------------------------------------------ native API

    def host(
        self,
        session: NativeSession,
        build: AgentBuild,
        *,
        factory: NativeAgentFactory | None = None,
    ) -> NativeAgentHost:
        """Build one native agent host over an existing native session.

        This is the native counterpart of create(): it resolves the preset and
        its materialized revision, builds the scoped and filtered tool registry,
        resolves the native provider and prompt source, returns a
        NativeAgentHost, persists the rendered system prompt and fires the
        creation observers. The host is remembered in the live set, so
        live_agents()/is_live() keep working for native hosts too.

        factory is the test seam (and the embedder seam): omit it to use the
        prompt, preset, tools and llm services this plugin was constructed with.
        """
        resolved = factory if factory is not None else self._native_factory()
        host = resolved.build(session, build)
        self._register(host)
        return host

    def _native_factory(self) -> NativeAgentFactory:
        """Build the default native factory from this plugin's services."""
        llm = lookup_service(self._ctx, LlmPlugin) if hasattr(self._ctx, "get") else None
        if llm is None:
            raise PluginDependencyError(
                "the agents service needs the llm service to build a native agent host"
            )
        return NativeAgentFactory(
            prompt=self._prompt_service,
            presets=self._presets,
            llm=llm,
            tools=self._tools,
            default_model=self._agent_config.model,
            default_max_steps=self._agent_config.max_steps,
            ptc_mode=self._legacy_ptc_mode,
        )

    def _legacy_ptc_mode(self) -> bool:
        """Whether the host-wide PTC switch is on (deprecated preset coupling)."""
        ptc = lookup_service(self._ctx, PtcRuntime) if hasattr(self._ctx, "get") else None
        return ptc is not None and getattr(ptc, "mode", "native") == "ptc"
