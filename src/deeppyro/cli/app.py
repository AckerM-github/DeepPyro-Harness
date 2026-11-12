"""Application assembly for the terminal front end.

This module is the CLI's only seam to the rest of DeepPyro. It boots the
**kernel** (deeppyro.kernel.boot) and resolves the stable services from the
resulting app instead of importing and wiring domain modules by hand:

======== ===============================================================
config   KernelConfig (validated Config)
frontend TerminalFrontend -- the single terminal presentation service
llm      .provider(name) -> LlmProvider
sessions .store -> SessionStore (create/open/list)
sandbox  .gate(workspace, ask) -> PermissionGate
tools    .registry_for(workspace) -> ToolRegistry
agents   .create(...), .options(**overrides) -> AgentSession/AgentOptions
======== ===============================================================

Workspace-scoped objects (permission gate, tool registry, agent session) are
built per command, never cached globally. Commands are synchronous, so
resolve_awaitable() bridges to the kernel's async lifecycle; CliApp.aclose()
disposes the booted app in reverse order.

build_app and list_session_infos are module-level seams: tests patch them to
inject fakes, which keeps the front end verifiable while the domain workstreams
land.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.prompt import Prompt
from rich.text import Text

from deeppyro.cli.frontend import TerminalFrontend
from deeppyro.cli.render import short_session_id
from deeppyro.config import Config, load_config
from deeppyro.core.errors import ConfigError, DeeppyroError

DEFAULT_CONTEXT_WINDOW = 32768


def resolve_workspace(path: str | Path | None = None) -> Path:
    """Resolve --cwd (or the process cwd) and reject missing directories."""
    workspace = Path(path).expanduser() if path else Path.cwd()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ConfigError(f"workspace directory does not exist: {workspace}")
    return workspace


async def await_maybe(value: Any) -> Any:
    """Await an awaitable, or return a plain value (sync test fakes)."""
    if inspect.isawaitable(value):
        return await value
    return value


def resolve_awaitable(value: Any) -> Any:
    """Return value or, if it is awaitable, its result via asyncio.run.

    Commands stay synchronous (typer callbacks are sync); the kernel lifecycle
    is async. Tests patch the seams with sync fakes, so both shapes work.
    """
    if not inspect.isawaitable(value):
        return value
    try:
        return asyncio.run(value)
    except RuntimeError:  # pragma: no cover - a loop cannot be running in a CLI
        if inspect.iscoroutine(value):
            value.close()
        raise


def _context_window(provider_cfg: Any, model_id: str) -> int:
    """Pick the model context window, falling back to the provider default."""
    models = list(getattr(provider_cfg, "models", []) or [])
    for entry in models:
        if getattr(entry, "id", None) == model_id and getattr(entry, "contextWindow", 0):
            return int(entry.contextWindow)
    if models and getattr(models[0], "contextWindow", 0):
        return int(models[0].contextWindow)
    return DEFAULT_CONTEXT_WINDOW


def make_terminal_ask(
    frontend: TerminalFrontend,
) -> Callable[[Any], Awaitable[bool]]:
    """Build the PermissionGate ask callback as an interactive terminal y/n.

    Non-interactive consoles (pipes, --json, tests) deny by default; blocking on
    a prompt nobody can see would hang the command.
    """

    async def ask(request: Any) -> bool:
        if not frontend.console.is_terminal:
            frontend.warning(f"approval denied (non-interactive): {getattr(request, 'summary', '')}")
            return False
        kind = getattr(request, "kind", "approval")
        summary = getattr(request, "summary", "")
        detail = getattr(request, "detail", "")
        line = Text(f"[{kind}] ", style="bold yellow")
        line.append(summary)
        frontend.console.print(line)
        if detail:
            frontend.console.print(Text(detail, style="dim"))
        answer = Prompt.ask(
            "allow?",
            choices=["y", "n"],
            default="n",
            console=frontend.console,
        )
        return answer == "y"

    return ask


@dataclass
class CliApp:
    """Fully booted runtime handed to the run and chat commands."""

    cfg: Config
    cwd: Path
    provider_name: str
    model: str
    context_window: int
    store: Any
    provider: Any
    registry: Any
    permission: Any
    frontend: TerminalFrontend
    agents: Any = None
    kernel_app: Any = None
    _closed: bool = field(default=False, repr=False)

    def new_session(self) -> Any:
        """Create a session in the configured store for the current workspace."""
        return self.store.create(str(self.cwd))

    def open_session(self, session_id: str) -> Any:
        """Open an existing session; raises SessionError when it does not exist."""
        return self.store.open(session_id)

    def list_sessions(self, *, limit: int = 20) -> list[Any]:
        """Return the most recent sessions, newest first."""
        return list(self.store.list())[: max(limit, 0)]

    def create_agent(self, session: Any, sink: Any, *, max_steps: int | None = None) -> Any:
        """Ask the agents service for the AgentSession used by run/chat."""
        overrides: dict[str, Any] = {"context_window": self.context_window}
        if max_steps is not None:
            overrides["max_steps"] = max_steps
        options = self.agents.options(**overrides)
        return self.agents.create(
            session=session,
            provider=self.provider,
            registry=self.registry,
            options=options,
            sink=sink,
            permission=self.permission,
            cwd=str(self.cwd),
        )

    def session_label(self, session: Any) -> str:
        """Short, human-facing session label used by banners and footers."""
        return short_session_id(getattr(session, "session_id", None))

    async def aclose(self) -> None:
        """Dispose the booted kernel app (reverse plugin order, best effort).

        Async because the kernel is disposed on the same event loop that ran the
        turn; call it with `await` from the command coroutines.
        """
        if self._closed:
            return
        self._closed = True
        kernel_app = self.kernel_app
        if kernel_app is not None:
            try:
                await await_maybe(kernel_app.stop())
            except DeeppyroError:  # pragma: no cover - shutdown must not mask exit codes
                pass
            self.kernel_app = None


async def boot_services(config: Config | None = None) -> Any:
    """Boot the kernel bundle (frontend, llm, sessions, sandbox, tools, agents)."""
    from deeppyro.kernel import boot

    return await boot(config)


def _resolve_model(cfg: Config, provider_name: str, *, provider: str | None, model: str | None) -> str:
    """Resolve the model id from flags, agent config, or the provider's first model."""
    provider_cfg = cfg.providers[provider_name]
    if provider is None and model is None:
        try:
            _, resolved_model = cfg.resolve()
        except KeyError as exc:  # defensive: provider_name was validated above
            raise ConfigError(f"unknown provider: {exc.args[0]}") from exc
    else:
        resolved_model = cfg.agent.model or (provider_cfg.models[0].id if provider_cfg.models else "")
    return model or resolved_model


async def build_app(
    *,
    provider: str | None = None,
    model: str | None = None,
    cwd: str | Path | None = None,
    frontend: TerminalFrontend | None = None,
    config: Config | None = None,
) -> CliApp:
    """Boot the kernel and resolve the per-command runtime for run/chat.

    Raises ConfigError for anything the user can fix on the command line or in
    config (unknown provider, missing workspace); the command layer maps that to
    exit code 2. A partially booted kernel is disposed before propagating.
    """
    cfg = config if config is not None else load_config()
    workspace = resolve_workspace(cwd)
    provider_name = provider or cfg.agent.provider
    if provider_name not in cfg.providers:
        raise ConfigError(f"unknown provider: {provider_name}")
    model_id = _resolve_model(cfg, provider_name, provider=provider, model=model)

    kernel_app = await boot_services(cfg)
    try:
        services = {
            name: kernel_app.service(name)
            for name in ("frontend", "llm", "sessions", "sandbox", "tools", "agents")
        }
        # The kernel service is the plugin, whose presentation object lives on
        # `.frontend`; tests may inject a TerminalFrontend directly.
        frontend_service = services["frontend"]
        ui = frontend if frontend is not None else getattr(frontend_service, "frontend", frontend_service)
        registry = services["tools"].registry_for(workspace)
        permission = services["sandbox"].gate(workspace, ask=make_terminal_ask(ui))
        llm_provider = services["llm"].provider(provider_name)
    except BaseException:
        await kernel_app.stop()
        raise

    return CliApp(
        cfg=cfg,
        cwd=workspace,
        provider_name=provider_name,
        model=model_id,
        context_window=_context_window(cfg.providers[provider_name], model_id),
        store=services["sessions"].store,
        provider=llm_provider,
        registry=registry,
        permission=permission,
        frontend=ui,
        agents=services["agents"],
        kernel_app=kernel_app,
    )


async def list_session_infos_async(config: Config | None = None) -> list[Any]:
    """List sessions through the kernel-managed store (boots and disposes).

    Async variant for callers already inside an event loop; the synchronous
    list_session_infos wrapper below is what typer uses.
    """
    kernel_app = await boot_services(config)
    try:
        return kernel_app.service("sessions").list()
    finally:
        await kernel_app.stop()


def list_session_infos(config: Config | None = None) -> list[Any]:
    """List sessions through the kernel-managed store (boots and disposes)."""
    return resolve_awaitable(list_session_infos_async(config))
