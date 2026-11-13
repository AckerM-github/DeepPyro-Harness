"""One-shot headless mode: the non-interactive counterpart of the TUI.

Running dpyro with a prompt (or --headless) executes exactly one turn and
exits, which keeps the harness scriptable (CI checks, pipes, quick questions)
without duplicating the terminal UI. Two output modes exist:

* rich streaming (default): assistant text and tool activity are printed while
  the model streams, followed by a compact footer;
* --json: one compact JSON object per line (NDJSON) on stdout, nothing else.

Exit codes are part of the contract:

* 0  success
* 1  runtime error (LLM/session/tool failure)
* 2  usage or configuration error (no prompt, bad flag, unknown provider/cwd)
* 130 interrupted by the user (Ctrl+C)

The module owns its sinks (the agent loop only receives an injected EventSink)
and boots the same cordis kernel as the TUI, so every mode shares one service
graph and one domain behaviour.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from deeppyro.config import Config, load_config
from deeppyro.core.errors import ConfigError, DeeppyroError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_CONTEXT_WINDOW = 32768
THINKING_MARKER = "· thinking"
USAGE_KEYS = ("input", "output", "cached")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def read_stdin_prompt() -> str:
    """Read a prompt from a piped stdin; interactive stdin counts as missing."""
    stream = sys.stdin
    if stream is None:
        return ""
    try:
        if stream.isatty():
            return ""
        return stream.read()
    except OSError:  # pragma: no cover - defensive, stdin may be closed
        return ""


def compact_text(text: str, *, max_lines: int = 3, max_chars: int = 300) -> str:
    """Collapse a possibly huge payload into a short single-block preview."""
    if not text:
        return ""
    lines = text.splitlines()
    preview = "\n".join(lines[:max_lines]).strip()
    truncated = len(lines) > max_lines or len(text) > max_chars
    if len(preview) > max_chars:
        preview = preview[: max_chars - 1] + "…"
    elif truncated:
        preview += " …"
    return preview


def resolve_workspace(path: str | Path | None = None) -> Path:
    """Resolve the workspace directory, rejecting missing paths."""
    workspace = Path(path).expanduser() if path else Path.cwd()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise ConfigError(f"workspace directory does not exist: {workspace}")
    return workspace


async def await_maybe(value: Any) -> Any:
    """Await an awaitable or return a plain value (test doubles)."""
    if inspect.isawaitable(value):
        return await value
    return value


def _context_window(provider_cfg: Any, model_id: str) -> int:
    models = list(getattr(provider_cfg, "models", []) or [])
    for entry in models:
        if getattr(entry, "id", None) == model_id and getattr(entry, "contextWindow", 0):
            return int(entry.contextWindow)
    if models and getattr(models[0], "contextWindow", 0):
        return int(models[0].contextWindow)
    return DEFAULT_CONTEXT_WINDOW


def _resolve_model(cfg: Config, provider_name: str, *, model: str | None) -> str:
    """Resolve the model without leaking [agent].model onto another provider."""
    provider_cfg = cfg.providers[provider_name]
    available = [str(entry.id) for entry in list(provider_cfg.models or [])]
    if model:
        if available and model not in available:
            raise ConfigError(
                f"unknown model: {model} (provider {provider_name} offers {', '.join(available)})"
            )
        return model
    if provider_name == cfg.agent.provider and cfg.agent.model:
        return cfg.agent.model
    if available:
        return available[0]
    if provider_name == cfg.agent.provider:
        try:
            _, resolved = cfg.resolve()
        except KeyError as exc:
            raise ConfigError(f"unknown provider: {exc.args[0]}") from exc
        return resolved
    return ""


async def _denying_ask(request: Any) -> bool:
    """Approval callback for one-shot mode: never block on an invisible prompt."""
    summary = getattr(request, "summary", "") or getattr(request, "kind", "")
    print(f"approval denied (headless mode): {summary}", file=sys.stderr)
    return False


# --------------------------------------------------------------------------
# --json sink (NDJSON)
# --------------------------------------------------------------------------


def _json_default(value: Any) -> str:
    return str(value)


class JsonLinesSink:
    """EventSink that writes one compact JSON object per line on stdout."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream
        self.event_count = 0
        self.saw_error = False

    @property
    def stream(self) -> TextIO:
        return self._stream if self._stream is not None else sys.stdout

    def begin_turn(self) -> None:
        """No-op: NDJSON has no streaming block state."""

    def end_turn(self) -> None:
        """No-op: every event is already one complete line."""

    def emit(self, event: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"), default=_json_default)
        out = self.stream
        out.write(payload + "\n")
        out.flush()
        self.event_count += 1
        if str(event.get("type", "")) == "error":
            self.saw_error = True


# --------------------------------------------------------------------------
# rich streaming sink
# --------------------------------------------------------------------------


class RichSink:
    """Render agent events to a Rich console while they stream."""

    def __init__(
        self,
        console: Console,
        *,
        show_reasoning: bool = True,
        err_console: Console | None = None,
    ) -> None:
        self.console = console
        self.err_console = err_console or console
        self._show_reasoning = show_reasoning
        self._mode: str | None = None
        self._text = ""
        self._final_text = ""
        self._wrote_text = False
        self._tool_names: dict[str, str] = {}
        self._usage: dict[str, int] | None = None
        self._session_id: str | None = None
        self.event_count = 0
        self.saw_error = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def usage(self) -> dict[str, int] | None:
        return dict(self._usage) if self._usage is not None else None

    @property
    def final_text(self) -> str:
        return self._final_text

    @property
    def saw_text(self) -> bool:
        return self._wrote_text

    def begin_turn(self) -> None:
        self._mode = None
        self._text = ""
        self._final_text = ""
        self._wrote_text = False
        self._tool_names = {}
        self._usage = None

    def end_turn(self) -> None:
        self._finish_block()

    def emit(self, event: Mapping[str, Any]) -> None:
        self.event_count += 1
        kind = str(event.get("type", ""))
        if kind == "text":
            self._on_text(str(event.get("text") or ""))
        elif kind == "thinking":
            self._on_thinking(str(event.get("text") or ""))
        elif kind == "tool_call":
            self._on_tool_call(event)
        elif kind == "tool_result":
            self._on_tool_result(event)
        elif kind == "usage":
            self._on_usage(event)
        elif kind == "final":
            self._on_final(str(event.get("text") or ""))
        elif kind == "error":
            self._on_error(str(event.get("message") or "unknown error"))
        elif kind == "session":
            self._session_id = str(event.get("sessionId") or "") or None
        # status and unknown event kinds carry no rendering payload

    def _on_text(self, chunk: str) -> None:
        if not chunk:
            return
        self._start_block("text")
        self._text += chunk
        self._wrote_text = True
        self.console.print(chunk, end="", markup=False, highlight=False)

    def _on_thinking(self, chunk: str) -> None:
        if not chunk or not self._show_reasoning:
            return
        self._start_block("thinking")
        self.console.print(chunk, end="", style="dim", markup=False, highlight=False)

    def _on_tool_call(self, event: Mapping[str, Any]) -> None:
        self._finish_block()
        name = str(event.get("name") or "tool")
        call_id = str(event.get("callId") or "")
        if call_id:
            self._tool_names[call_id] = name
        args = compact_text(str(event.get("arguments") or ""), max_lines=1, max_chars=160)
        line = Text("→ ", style="cyan")
        line.append(name, style="bold cyan")
        if args:
            line.append(" " + args, style="dim")
        self.console.print(line)

    def _on_tool_result(self, event: Mapping[str, Any]) -> None:
        self._finish_block()
        call_id = str(event.get("callId") or "")
        name = self._tool_names.get(call_id, "tool")
        is_error = bool(event.get("isError"))
        preview = compact_text(str(event.get("text") or ""))
        line = Text("  ✗ " if is_error else "  ✓ ", style="red" if is_error else "green")
        line.append(name, style="dim")
        if preview:
            line.append("  " + preview, style="dim")
        self.console.print(line)

    def _on_usage(self, event: Mapping[str, Any]) -> None:
        values = {key: int(event.get(key) or 0) for key in USAGE_KEYS}
        if self._usage is None:
            self._usage = values
            return
        for key, value in values.items():
            self._usage[key] += value

    def _on_final(self, text: str) -> None:
        self._final_text = text
        if not self._wrote_text and text:
            self.console.print(text, markup=False, highlight=False)
            self._wrote_text = True
        self._finish_block()

    def _on_error(self, message: str) -> None:
        self.saw_error = True
        self._finish_block()
        line = Text("error: ", style="bold red")
        line.append(message, style="red")
        self.err_console.print(line)

    def _start_block(self, mode: str) -> None:
        if self._mode == mode:
            return
        self._finish_block()
        self._mode = mode
        if mode == "thinking":
            self.console.print(THINKING_MARKER, style="dim", markup=False)

    def _finish_block(self) -> None:
        if self._mode is not None:
            self.console.print()
        self._mode = None


# --------------------------------------------------------------------------
# booted runtime
# --------------------------------------------------------------------------


@dataclass
class HeadlessRuntime:
    """Booted kernel plus the workspace-scoped collaborators for one turn."""

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
    jobs: Any = None
    team: Any = None
    _closed: bool = field(default=False, repr=False)

    def create_agent(self, session: Any, sink: Any, *, max_steps: int | None = None) -> Any:
        overrides: dict[str, Any] = {
            "model": self.model,
            "context_window": self.context_window,
        }
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
            services=self._plugin_services(),
        )

    def _plugin_services(self) -> dict[str, Any]:
        """Plugin services the agent tools read from ToolContext.services."""
        services: dict[str, Any] = {}
        kernel_app = self.kernel_app
        if kernel_app is None:
            return services
        for name in ("plugin_manager", "inspect", "settings", "jobs", "team"):
            try:
                value = kernel_app.service(name)
            except LookupError:
                continue
            services[name] = value
        return services

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        team = self.team
        close_team = getattr(team, "close", None)
        if close_team is not None:
            try:
                await await_maybe(close_team())
            except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                pass
        kernel_app, self.kernel_app = self.kernel_app, None
        if kernel_app is not None:
            try:
                await await_maybe(kernel_app.stop())
            except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                pass


async def boot_runtime(
    *,
    config: Config | None = None,
    cwd: str | Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    ask: Callable[[Any], Awaitable[bool]] | None = None,
) -> HeadlessRuntime:
    """Boot the kernel bundle and resolve the services needed for one turn."""
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
        names = ("llm", "sessions", "sandbox", "tools", "agents")
        services = {name: kernel_app.service(name) for name in names}
        llm_provider = services["llm"].provider(provider_name)
        registry = services["tools"].registry_for(workspace)
        permission = services["sandbox"].gate(workspace, ask=ask or _denying_ask)
    except BaseException:
        await await_maybe(kernel_app.stop())
        raise
    jobs = None
    try:
        jobs = kernel_app.service("jobs")
    except LookupError:
        pass
    team = None
    try:
        team = kernel_app.service("team")
    except LookupError:
        pass
    runtime = HeadlessRuntime(
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
        jobs=jobs,
        team=team,
    )
    configure_team = getattr(team, "configure", None)
    if configure_team is not None:
        try:
            configure_team(
                cwd=workspace,
                provider=llm_provider,
                registry=registry,
                permission=permission,
                model=model_id,
                context_window=runtime.context_window,
                services=runtime._plugin_services(),
                ask=ask or _denying_ask,
            )
        except Exception:  # noqa: BLE001, S110 - the optional team service must not break headless
            pass
    return runtime


# --------------------------------------------------------------------------
# one-shot entry
# --------------------------------------------------------------------------


def run_once(
    prompt: str | None,
    *,
    model: str | None = None,
    provider: str | None = None,
    json_output: bool = False,
    cwd: str | None = None,
    max_steps: int | None = None,
    config: Config | None = None,
    console: Console | None = None,
) -> int:
    """Run one turn and return the documented exit code."""
    ui = console or Console()
    if max_steps is not None and max_steps < 1:
        ui.print("error: --max-steps must be at least 1", style="bold red")
        return EXIT_USAGE
    text = prompt if prompt is not None else read_stdin_prompt()
    if not text.strip():
        ui.print("error: no prompt given (pass a prompt or pipe one on stdin)", style="bold red")
        return EXIT_USAGE
    try:
        return asyncio.run(
            _run_async(
                text,
                model=model,
                provider=provider,
                json_output=json_output,
                cwd=cwd,
                max_steps=max_steps,
                config=config,
                console=ui,
            )
        )
    except KeyboardInterrupt:
        ui.print("interrupted", style="yellow")
        return EXIT_INTERRUPTED


async def _cancel_jobs(runtime: HeadlessRuntime, session_id: str) -> int:
    """Cancel every background job this session started (one-shot cleanup)."""
    jobs = getattr(runtime, "jobs", None)
    if jobs is None:
        return 0
    cancel_owner = getattr(jobs, "cancel_owner", None)
    if cancel_owner is None:
        return 0
    try:
        return int(await await_maybe(cancel_owner(session_id)) or 0)
    except Exception:  # noqa: BLE001 - cleanup must not change the exit code
        return 0


async def _run_async(
    text: str,
    *,
    model: str | None,
    provider: str | None,
    json_output: bool,
    cwd: str | None,
    max_steps: int | None,
    config: Config | None,
    console: Console,
) -> int:
    try:
        runtime = await boot_runtime(config=config, cwd=cwd, provider=provider, model=model)
    except ConfigError as exc:
        console.print(f"error: {exc}", style="bold red")
        return EXIT_USAGE
    except DeeppyroError as exc:
        console.print(f"error: {exc}", style="bold red")
        return EXIT_ERROR

    sink: Any = JsonLinesSink() if json_output else RichSink(console)
    try:
        session = runtime.store.create(str(runtime.cwd))
        session_id = str(getattr(session, "session_id", "") or "")
        session_label = session_id.removeprefix("session-")[:8]
        sink.emit({"type": "session", "sessionId": session_id})
        if not json_output:
            console.print(
                f"session {session_label}  {runtime.provider_name}/{runtime.model}  cwd {runtime.cwd}",
                style="dim",
            )
        team = getattr(runtime, "team", None)
        bind_team = getattr(team, "bind_session", None)
        if bind_team is not None:
            bind_team(session)
        agent = runtime.create_agent(session, sink, max_steps=max_steps)
        sink.begin_turn()
        try:
            await await_maybe(agent.send(text))
        except KeyboardInterrupt:
            try:
                await await_maybe(agent.interrupt())
            except Exception:  # noqa: BLE001, S110 - interruption is best effort
                pass
            sink.end_turn()
            console.print("interrupted", style="yellow")
            return EXIT_INTERRUPTED
        except DeeppyroError as exc:
            sink.end_turn()
            console.print(f"error: {exc}", style="bold red")
            return EXIT_ERROR
        finally:
            close_agent = getattr(agent, "aclose", None)
            if close_agent is not None:
                await await_maybe(close_agent())
            await _cancel_jobs(runtime, session_id)
        sink.end_turn()
        if not json_output:
            usage = getattr(sink, "usage", None) or {}
            parts = [f"session {session_label}"]
            if usage:
                parts.append(
                    "tokens in={input} out={output} cached={cached}".format(
                        input=usage.get("input", 0),
                        output=usage.get("output", 0),
                        cached=usage.get("cached", 0),
                    )
                )
            console.print(" · ".join(parts), style="dim")
        if getattr(sink, "saw_error", False):
            return EXIT_ERROR
        return EXIT_OK
    except (DeeppyroError, OSError) as exc:
        console.print(f"error: {exc}", style="bold red")
        return EXIT_ERROR
    finally:
        await runtime.aclose()


__all__ = [
    "EXIT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_USAGE",
    "HeadlessRuntime",
    "JsonLinesSink",
    "RichSink",
    "boot_runtime",
    "compact_text",
    "read_stdin_prompt",
    "resolve_workspace",
    "run_once",
]
