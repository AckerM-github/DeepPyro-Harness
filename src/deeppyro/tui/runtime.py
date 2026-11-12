"""Runtime assembly for the terminal UI.

The TUI boots the cordis kernel (the same bundle the one-shot headless path
uses) and resolves the services; everything workspace-scoped is built here once
and then reused for the whole UI session:

* one SessionStore for creating/opening sessions,
* one ToolRegistry bound to the workspace cwd,
* one PermissionGate whose approval callback is served by the UI modal,
* one LlmProvider,
* one AgentSession per active session.

This module is the TUI's only seam to the domain layers, mirroring
deeppyro.headless.boot_runtime.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.config import Config, load_config
from deeppyro.core.errors import ConfigError, DeeppyroError

DEFAULT_CONTEXT_WINDOW = 32768


def resolve_workspace(path: str | Path | None = None) -> Path:
    """Resolve the workspace directory, rejecting missing paths."""
    workspace = Path(path).expanduser() if path else Path.cwd()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ConfigError(f"workspace directory does not exist: {workspace}")
    return workspace


def _context_window(provider_cfg: Any, model_id: str) -> int:
    """Pick the model context window, falling back to the provider default."""
    models = list(getattr(provider_cfg, "models", []) or [])
    for entry in models:
        if getattr(entry, "id", None) == model_id and getattr(entry, "contextWindow", 0):
            return int(entry.contextWindow)
    if models and getattr(models[0], "contextWindow", 0):
        return int(models[0].contextWindow)
    return DEFAULT_CONTEXT_WINDOW


async def await_maybe(value: Any) -> Any:
    """Await an awaitable or return a plain value (test doubles)."""
    if inspect.isawaitable(value):
        return await value
    return value


@dataclass
class TuiRuntime:
    """Booted kernel plus the live session/agent pair for the TUI."""

    cfg: Config
    cwd: Path
    kernel_app: Any
    provider_name: str
    model: str
    context_window: int
    store: Any
    provider: Any
    registry: Any
    permission: Any
    agents: Any
    sink: Any
    ask: Callable[[Any], Awaitable[bool]]
    session: Any = None
    agent: Any = None
    _closed: bool = field(default=False, repr=False)

    # -- construction ---------------------------------------------------
    @classmethod
    async def boot(
        cls,
        *,
        sink: Any,
        ask: Callable[[Any], Awaitable[bool]],
        cwd: str | Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        config: Config | None = None,
    ) -> TuiRuntime:
        """Boot the kernel bundle and resolve the TUI runtime."""
        from deeppyro.kernel import boot

        cfg = config if config is not None else load_config()
        workspace = resolve_workspace(cwd)
        provider_name = provider or cfg.agent.provider
        if provider_name not in cfg.providers:
            raise ConfigError(f"unknown provider: {provider_name}")
        provider_cfg = cfg.providers[provider_name]
        model_id = _resolve_model(cfg, provider_name, model=model)
        kernel_app = await boot(cfg)
        try:
            services = {
                name: kernel_app.service(name) for name in ("llm", "sessions", "sandbox", "tools", "agents")
            }
            llm_provider = services["llm"].provider(provider_name)
            registry = services["tools"].registry_for(workspace)
            permission = services["sandbox"].gate(workspace, ask=ask)
        except BaseException:
            await await_maybe(kernel_app.stop())
            raise
        runtime = cls(
            cfg=cfg,
            cwd=workspace,
            kernel_app=kernel_app,
            provider_name=provider_name,
            model=model_id,
            context_window=_context_window(provider_cfg, model_id),
            store=services["sessions"].store,
            provider=llm_provider,
            registry=registry,
            permission=permission,
            agents=services["agents"],
            sink=sink,
            ask=ask,
        )
        await runtime.new_session()
        return runtime

    # -- sessions -------------------------------------------------------
    async def new_session(self) -> Any:
        """Create a fresh durable session and rebuild the agent."""
        session = self.store.create(str(self.cwd))
        await self._attach(session)
        return session

    async def open_session(self, session_id: str) -> Any:
        """Open an existing session and rebuild the agent around it."""
        session = self.store.open(session_id)
        await self._attach(session)
        return session

    async def _attach(self, session: Any) -> None:
        options = self.agents.options(
            model=self.model,
            context_window=self.context_window,
        )
        agent = self.agents.create(
            session=session,
            provider=self.provider,
            registry=self.registry,
            options=options,
            sink=self.sink,
            permission=self.permission,
            cwd=str(self.cwd),
        )
        await await_maybe(getattr(self.agent, "aclose", None))
        self.session = session
        self.agent = agent

    # -- model / session introspection ----------------------------------
    async def set_model(self, model_id: str) -> None:
        """Switch the model for subsequent turns and rebuild the agent."""
        if model_id == self.model:
            return
        provider_cfg = self.cfg.providers[self.provider_name]
        self.model = model_id
        self.context_window = _context_window(provider_cfg, model_id)
        if self.session is not None:
            await self._attach(self.session)

    @property
    def session_id(self) -> str:
        return str(getattr(self.session, "session_id", "") or "")

    @property
    def busy(self) -> bool:
        return bool(getattr(self.agent, "busy", False))

    def session_usage(self) -> dict[str, int]:
        """Cumulative token usage of the active session."""
        getter = getattr(self.session, "usage", None)
        if getter is None:
            return {"input": 0, "output": 0, "cached": 0}
        try:
            usage = getter()
        except Exception:  # noqa: BLE001 - usage display must never break the UI
            return {"input": 0, "output": 0, "cached": 0}
        return {key: int(usage.get(key, 0)) for key in ("input", "output", "cached")}

    def models(self) -> list[str]:
        """Model ids available on the active provider."""
        provider_cfg = self.cfg.providers[self.provider_name]
        return [entry.id for entry in list(provider_cfg.models or [])]

    def recent_sessions(self, limit: int = 20) -> list[Any]:
        """Most recent sessions, newest first."""
        return list(self.store.list())[: max(limit, 0)]

    async def compact_now(self) -> bool:
        """Compact the active session immediately; True when it shrank."""
        session, agent = self.session, self.agent
        if session is None or agent is None:
            return False
        compactor = getattr(agent, "compactor", None)
        if compactor is None:
            raise DeeppyroError("compaction is not available for this agent")
        messages = session.derive_messages()
        return bool(await compactor.compact(session, messages))

    # -- shutdown -------------------------------------------------------
    async def close(self) -> None:
        """Dispose the kernel app (reverse plugin order, best effort)."""
        if self._closed:
            return
        self._closed = True
        kernel_app, self.kernel_app = self.kernel_app, None
        if kernel_app is not None:
            try:
                await await_maybe(kernel_app.stop())
            except Exception:  # noqa: BLE001, S110 - shutdown must never mask an exception
                pass


def _resolve_model(cfg: Config, provider_name: str, *, model: str | None) -> str:
    """Resolve the model id from an override, agent config, or provider default."""
    provider_cfg = cfg.providers[provider_name]
    if model:
        return model
    if cfg.agent.model:
        return cfg.agent.model
    if provider_name == cfg.agent.provider:
        try:
            _, resolved = cfg.resolve()
        except KeyError as exc:
            raise ConfigError(f"unknown provider: {exc.args[0]}") from exc
        return resolved
    return provider_cfg.models[0].id if provider_cfg.models else ""


__all__ = ["DEFAULT_CONTEXT_WINDOW", "TuiRuntime", "await_maybe", "resolve_workspace"]
