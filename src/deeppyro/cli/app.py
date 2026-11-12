"""Application assembly for the terminal front end.

This module is the single seam between the CLI and the domain workstreams. It
performs the deferred imports (session store, llm provider, tool registry,
permission gate) only when a command actually needs them, so `--help`,
`--version`, `config show` and `sessions` keep working while those modules land,
and so tests can inject fakes through `deeppyro.cli.main.build_app`.

Two entry points are exported:

* `build_app` -- full wiring for `run` and `chat` (config + store + provider +
  tools + permissions).
* `open_session_store` -- store-only wiring for `sessions`, which must not
  require a network provider.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.prompt import Prompt
from rich.text import Text

from deeppyro.cli.frontend import TerminalFrontend
from deeppyro.cli.render import short_session_id
from deeppyro.config import Config, load_config, session_dir
from deeppyro.core.errors import ConfigError, DeeppyroError

DEFAULT_CONTEXT_WINDOW = 32768


def resolve_workspace(path: str | Path | None = None) -> Path:
    """Resolve --cwd (or the process cwd) and reject missing directories."""
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


def _drain(value: Any) -> None:
    """Run an awaitable returned by a synchronous CLI call, if any."""
    if not inspect.isawaitable(value):
        return
    try:
        asyncio.run(value)
    except RuntimeError:  # pragma: no cover - no loop can be running in a sync CLI
        if inspect.iscoroutine(value):
            value.close()


def make_terminal_ask(
    frontend: TerminalFrontend,
) -> Callable[[Any], Awaitable[bool]]:
    """Build the PermissionGate `ask` callback as an interactive terminal y/n.

    Non-interactive consoles (pipes, --json, tests) deny by default; the gate
    documents that behaviour and we mirror it here rather than blocking on a
    prompt nobody can see.
    """

    async def ask(request: Any) -> bool:
        if not frontend.console.is_terminal:
            frontend.warning(
                f"approval denied (non-interactive): {getattr(request, 'summary', '')}"
            )
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
    """Fully wired runtime handed to the `run` and `chat` commands."""

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

    def new_session(self) -> Any:
        """Create a session in the configured store for the current workspace."""
        return self.store.create(str(self.cwd))

    def open_session(self, session_id: str) -> Any:
        """Open an existing session; raises SessionError when it does not exist."""
        return self.store.open(session_id)

    def list_sessions(self, *, limit: int = 20) -> list[Any]:
        """Return the most recent sessions, newest first."""
        return list(self.store.list())[: max(limit, 0)]

    def create_agent(
        self, session: Any, sink: Any, *, max_steps: int | None = None
    ) -> Any:
        """Build the AgentSession used by run/chat, deferring the agent import."""
        from deeppyro.agent import AgentOptions, AgentSession

        options = AgentOptions(
            max_steps=max_steps if max_steps is not None else self.cfg.agent.maxSteps,
            max_tokens=self.cfg.agent.maxTokens,
            temperature=self.cfg.agent.temperature,
            context_window=self.context_window,
        )
        return AgentSession(
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

    def aclose(self) -> None:
        """Close the LLM provider, draining an async aclose if the provider has one."""
        closer = getattr(self.provider, "aclose", None)
        if closer is None:
            return
        try:
            _drain(closer())
        except DeeppyroError:  # pragma: no cover - shutdown must not mask the exit code
            pass


def build_app(
    *,
    provider: str | None = None,
    model: str | None = None,
    cwd: str | Path | None = None,
    frontend: TerminalFrontend | None = None,
) -> CliApp:
    """Load config and wire the full runtime used by `run` and `chat`.

    Raises ConfigError for anything the user can fix on the command line or in
    config (unknown provider, missing workspace); the command layer maps that to
    exit code 2.
    """
    frontend = frontend or TerminalFrontend()
    cfg = load_config()
    workspace = resolve_workspace(cwd)
    provider_name = provider or cfg.agent.provider
    if provider_name not in cfg.providers:
        raise ConfigError(f"unknown provider: {provider_name}")
    provider_cfg = cfg.providers[provider_name]
    if provider is None and model is None:
        try:
            _, resolved_model = cfg.resolve()
        except KeyError as exc:  # defensive: provider_name was validated above
            raise ConfigError(f"unknown provider: {exc.args[0]}") from exc
    else:
        resolved_model = cfg.agent.model or (
            provider_cfg.models[0].id if provider_cfg.models else ""
        )
    model_id = model or resolved_model

    from deeppyro.llm import create_provider
    from deeppyro.sandbox import PermissionGate
    from deeppyro.session import SessionStore
    from deeppyro.tools import ToolRegistry, builtin_tools

    store = SessionStore(root=str(session_dir(cfg)))
    registry = ToolRegistry(tools=builtin_tools(cfg, workspace))
    permission = PermissionGate(
        cfg.agent.permissionPreset,
        cfg.agent.approvalPolicy,
        workspace,
        ask=make_terminal_ask(frontend),
    )
    llm = create_provider(provider_name, provider_cfg)
    return CliApp(
        cfg=cfg,
        cwd=workspace,
        provider_name=provider_name,
        model=model_id,
        context_window=_context_window(provider_cfg, model_id),
        store=store,
        provider=llm,
        registry=registry,
        permission=permission,
        frontend=frontend,
    )


def open_session_store(cfg: Config | None = None) -> Any:
    """Open the session store only (used by `dpyro sessions`)."""
    if cfg is None:
        cfg = load_config()
    from deeppyro.session import SessionStore

    return SessionStore(root=str(session_dir(cfg)))
