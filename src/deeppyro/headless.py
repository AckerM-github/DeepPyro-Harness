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
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from rich.console import Console
from rich.text import Text

from deeppyro.api import DEFAULT_PRESET_ID, PluginDependencyError
from deeppyro.config import Config, load_config
from deeppyro.core.errors import ConfigError, DeeppyroError
from deeppyro.hooks import SESSION_CREATED, SESSION_DISPOSED

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_CONTEXT_WINDOW = 32768
THINKING_MARKER = "· thinking"
USAGE_KEYS = ("input", "output", "cached")


class UnknownPresetError(ConfigError):
    """A requested agent preset id is not registered in this build."""


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


def _resolve_model(
    cfg: Config,
    provider_name: str,
    *,
    model: str | None,
    allow_unknown: bool = False,
) -> str:
    """Resolve the model without leaking [agent].model onto another provider.

    With `allow_unknown` an explicit model that is not declared in config is
    returned as-is so boot_runtime can look it up in the provider's discovered
    catalog (an unknown model is only an error after that lookup).
    """
    provider_cfg = cfg.providers[provider_name]
    available = [str(entry.id) for entry in list(provider_cfg.models or [])]
    if model:
        if available and model not in available and not allow_unknown:
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


def _resolve_preset(presets: Any, requested: str | None) -> tuple[str, Any]:
    """Canonicalize and resolve one agent preset, defaulting to the standard one.

    Returns the canonical preset id and the resolved spec (None when the booted
    bundle carries no presets service, e.g. a minimal test bundle). An unknown
    id surfaces as UnknownPresetError, which the one-shot entry point reports as
    a usage error on stderr instead of a traceback.
    """
    if presets is None:
        return (requested or "").strip() or DEFAULT_PRESET_ID, None
    canonical = presets.canonical(requested) or DEFAULT_PRESET_ID
    try:
        spec = presets.resolve(canonical)
    except PluginDependencyError as exc:
        raise UnknownPresetError(str(exc)) from exc
    return str(getattr(spec, "id", "") or canonical), spec


def _configured_model(provider_cfg: Any, model_id: str) -> bool:
    """Whether config.toml declares this model for the provider."""
    return any(
        str(getattr(entry, "id", "")) == model_id for entry in list(getattr(provider_cfg, "models", []) or [])
    )


def _unknown_model_error(provider_name: str, model_id: str, provider_cfg: Any) -> ConfigError:
    """The established unknown-model error (kept identical to pre-catalog mode)."""
    available = [str(entry.id) for entry in list(getattr(provider_cfg, "models", []) or [])]
    return ConfigError(f"unknown model: {model_id} (provider {provider_name} offers {', '.join(available)})")


def _catalog_error(llm_service: Any) -> BaseException | None:
    """Most recent discovery failure recorded by the llm service, if exposed."""
    registry = getattr(llm_service, "registry", None)
    if registry is None:
        return None
    return getattr(registry, "last_catalog_error", None)


async def _runtime_context_window(
    llm_service: Any,
    provider_cfg: Any,
    provider_name: str,
    model_id: str,
    *,
    discover: bool,
) -> int:
    """Pick the context window for a resolved model, discovering catalog-only ids.

    Models declared in config.toml never touch the network: their configured
    (or first configured) window is authoritative. An explicit `--model` that is
    not declared is resolved through the post-boot llm catalog (models discovered
    from the provider's listing endpoint). The rules are:

    * catalog says the model exists -> use its `context_window`;
    * catalog definitively does not list it -> ConfigError (existing semantics);
    * discovery itself failed (network/timeout) or the service cannot discover ->
      fall back to DEFAULT_CONTEXT_WINDOW rather than failing the whole run.
    """
    if not discover or _configured_model(provider_cfg, model_id):
        return _context_window(provider_cfg, model_id)
    lookup = getattr(llm_service, "model_info", None)
    if lookup is None:
        raise _unknown_model_error(provider_name, model_id, provider_cfg)
    try:
        info = await await_maybe(lookup(provider_name, model_id))
    except Exception:  # noqa: BLE001 - catalog discovery must not block startup
        return DEFAULT_CONTEXT_WINDOW
    if info is not None:
        window = getattr(info, "context_window", None)
        if window is not None and int(window) > 0:
            return int(window)
        # The model exists but the listing carries no window: stay conservative
        # instead of borrowing an unrelated configured model's window.
        return DEFAULT_CONTEXT_WINDOW
    if _catalog_error(llm_service) is not None:
        # The lookup was inconclusive (the listing failed), so we cannot claim the
        # model is unknown; prefer booting with a conservative window over exit 2.
        return DEFAULT_CONTEXT_WINDOW
    raise _unknown_model_error(provider_name, model_id, provider_cfg)


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
    subagent: Any = None
    working_directory: Any = None
    user_questions: Any = None
    projections: Any = None
    tools: Any = None
    preset: str | None = None
    preset_spec: Any = None
    _closed: bool = field(default=False, repr=False)

    def create_agent(self, session: Any, sink: Any, *, max_steps: int | None = None) -> Any:
        overrides: dict[str, Any] = {
            "model": self.model,
            "context_window": self.context_window,
        }
        if max_steps is not None:
            overrides["max_steps"] = max_steps
        options = self.agents.options(**overrides)
        preset_id = self.preset or DEFAULT_PRESET_ID
        revision = getattr(self.preset_spec, "revision", None)
        registry = self.registry
        scoped = getattr(self.tools, "registry_for", None)
        if callable(scoped):
            registry = scoped(str(self.cwd), scope=preset_id, revision=revision)
        return self.agents.create(
            session=session,
            provider=self.provider,
            registry=registry,
            options=options,
            sink=sink,
            permission=self.permission,
            cwd=str(self.cwd),
            preset=preset_id,
            preset_revision=revision,
            services=self._plugin_services(),
        )

    def bind_session(self, session: Any) -> None:
        """Bind per-session callbacks for services that record into the log."""
        if session is None:
            return
        service = self.working_directory
        bind = getattr(service, "bind", None)
        if bind is not None:
            try:
                bind(
                    session,
                    cwd=self.cwd,
                    record=session.append,
                    inject_notice=lambda text: session.inject(text, source={"kind": "working-directory"}),
                )
            except Exception:  # noqa: BLE001, S110 - optional service must not break headless
                pass
        self._restore_projections(session)

    def _restore_projections(self, session: Any) -> None:
        """Restore the projection sidecar and observe appends for this session."""
        service = self.projections
        attach = getattr(service, "attach", None)
        if attach is None:
            return
        try:
            attach(session)
        except Exception:  # noqa: BLE001, S110 - optional service must not break headless
            pass

    def save_projections(self, session: Any) -> None:
        """Write the projection sidecar and stop observing the session."""
        service = self.projections
        if service is None or session is None:
            return
        save = getattr(service, "save", None)
        detach = getattr(service, "detach", None)
        if save is not None:
            try:
                save(session)
            except Exception:  # noqa: BLE001, S110 - optional service must not break headless
                pass
        if detach is not None:
            try:
                detach(session)
            except Exception:  # noqa: BLE001, S110 - optional service must not break headless
                pass

    def _plugin_services(self) -> dict[str, Any]:
        """Plugin services the agent tools read from ToolContext.services."""
        services: dict[str, Any] = {}
        kernel_app = self.kernel_app
        if kernel_app is None:
            return services
        for name in (
            "llm",
            "hooks",
            "retryPolicy",
            "plugin_manager",
            "inspect",
            "settings",
            "jobs",
            "team",
            "subagent",
            "working_directory",
            "user_questions",
            "attachments",
            "schedule",
            "memory",
            "ptc",
        ):
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
        subagent = self.subagent
        close_subagent = getattr(subagent, "close", None)
        if close_subagent is not None:
            try:
                await await_maybe(close_subagent())
            except Exception:  # noqa: BLE001, S110 - shutdown must not mask an exception
                pass
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
    preset: str | None = None,
    ask: Callable[[Any], Awaitable[bool]] | None = None,
) -> HeadlessRuntime:
    """Boot the kernel bundle and resolve the services needed for one turn.

    An explicit agent preset is canonicalized and resolved here, before any
    session exists, so an unknown id fails the boot cleanly; plugin realms of
    the selected revision are materialized before an agent can be created.
    """
    from deeppyro.kernel import boot

    cfg = config if config is not None else load_config()
    workspace = resolve_workspace(cwd)
    provider_name = provider or cfg.agent.provider
    if provider_name not in cfg.providers:
        raise ConfigError(f"unknown provider: {provider_name}")
    provider_cfg = cfg.providers[provider_name]
    explicit_model = bool(model)
    model_id = _resolve_model(cfg, provider_name, model=model, allow_unknown=explicit_model)
    kernel_app = await boot(cfg)
    try:
        names = ("llm", "sessions", "sandbox", "tools", "agents")
        services = {name: kernel_app.service(name) for name in names}
        llm_provider = services["llm"].provider(provider_name)
        context_window = await _runtime_context_window(
            services["llm"],
            provider_cfg,
            provider_name,
            model_id,
            discover=explicit_model,
        )
        registry = services["tools"].registry_for(workspace)
        permission = services["sandbox"].gate(workspace, ask=ask or _denying_ask)
        try:
            presets_service = kernel_app.service("presets")
        except LookupError:
            presets_service = None
        preset_id, preset_spec = _resolve_preset(presets_service, preset)
        ensure = getattr(presets_service, "ensure", None)
        if preset_spec is not None and callable(ensure):
            await await_maybe(ensure(preset_spec.id, revision=getattr(preset_spec, "revision", None)))
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
    subagent = None
    try:
        subagent = kernel_app.service("subagent")
    except LookupError:
        pass
    working_directory = None
    try:
        working_directory = kernel_app.service("working_directory")
    except LookupError:
        pass
    user_questions = None
    try:
        user_questions = kernel_app.service("user_questions")
    except LookupError:
        pass
    projections = None
    try:
        projections = kernel_app.service("projections")
    except LookupError:
        pass
    runtime = HeadlessRuntime(
        cfg=cfg,
        cwd=workspace,
        kernel_app=kernel_app,
        provider_name=provider_name,
        model=model_id,
        context_window=context_window,
        store=services["sessions"].store,
        provider=llm_provider,
        registry=registry,
        permission=permission,
        agents=services["agents"],
        jobs=jobs,
        team=team,
        subagent=subagent,
        working_directory=working_directory,
        user_questions=user_questions,
        projections=projections,
        tools=services["tools"],
        preset=preset_id,
        preset_spec=preset_spec,
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
    configure_subagent = getattr(subagent, "configure", None)
    if configure_subagent is not None:
        try:
            configure_subagent(
                cwd=workspace,
                provider=llm_provider,
                registry=registry,
                permission=permission,
                model=model_id,
                context_window=runtime.context_window,
                services=runtime._plugin_services(),
                ask=ask or _denying_ask,
            )
        except Exception:  # noqa: BLE001, S110 - the optional subagent service must not break headless
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
    preset: str | None = None,
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
                preset=preset,
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


def _hooks_service(runtime: Any) -> Any | None:
    """Resolve the kernel hook service, tolerating runtimes without a kernel."""
    app = getattr(runtime, "kernel_app", None)
    service = getattr(app, "service", None)
    if service is None:
        return None
    try:
        return service("hooks")
    except LookupError:
        return None


async def _emit_hook(runtime: Any, name: str, payload: dict[str, Any]) -> None:
    """Emit one session-lifecycle hook; observability must never break a run."""
    hooks = _hooks_service(runtime)
    if hooks is None or not hooks.has(name):
        return
    try:
        await hooks.emit(name, payload)
    except Exception:
        logger.exception("hook %r dispatch failed", name)


async def _run_async(
    text: str,
    *,
    model: str | None,
    provider: str | None,
    preset: str | None,
    json_output: bool,
    cwd: str | None,
    max_steps: int | None,
    config: Config | None,
    console: Console,
) -> int:
    try:
        runtime = await boot_runtime(config=config, cwd=cwd, provider=provider, model=model, preset=preset)
    except UnknownPresetError as exc:
        # A bad --preset is a usage error, not a runtime failure: no traceback,
        # no session, and a message a shell script can grep for.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ConfigError as exc:
        console.print(f"error: {exc}", style="bold red")
        return EXIT_USAGE
    except DeeppyroError as exc:
        console.print(f"error: {exc}", style="bold red")
        return EXIT_ERROR

    sink: Any = JsonLinesSink() if json_output else RichSink(console)
    session_id: str | None = None
    preset_id = getattr(runtime, "preset", None) or DEFAULT_PRESET_ID
    try:
        session = runtime.store.create(str(runtime.cwd), agent_preset=preset_id)
        session_id = str(getattr(session, "session_id", "") or "")
        if session_id:
            await _emit_hook(
                runtime,
                SESSION_CREATED,
                {"sessionId": session_id, "cwd": str(runtime.cwd), "preset": preset_id},
            )
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
        subagent = getattr(runtime, "subagent", None)
        bind_subagent = getattr(subagent, "bind_session", None)
        if bind_subagent is not None:
            bind_subagent(session)
        bind_working_directory = getattr(runtime, "bind_session", None)
        if bind_working_directory is not None:
            bind_working_directory(session)
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
            save_projections = getattr(runtime, "save_projections", None)
            if save_projections is not None:
                save_projections(session)
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
        if session_id:
            await _emit_hook(runtime, SESSION_DISPOSED, {"sessionId": session_id})
        await runtime.aclose()


__all__ = [
    "EXIT_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_USAGE",
    "HeadlessRuntime",
    "JsonLinesSink",
    "RichSink",
    "UnknownPresetError",
    "boot_runtime",
    "compact_text",
    "read_stdin_prompt",
    "resolve_workspace",
    "run_once",
]
