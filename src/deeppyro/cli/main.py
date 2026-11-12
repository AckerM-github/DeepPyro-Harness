"""The dpyro command line entry point.

Only this module imports typer. Commands parse arguments, hand the work to the
single terminal front end (cli.frontend / cli.render / cli.chat) and translate
outcomes into the documented exit codes:

* 0  success
* 1  runtime error (LLM/session/tool failure)
* 2  usage or configuration error (bad flag, unknown provider/session/invalid --cwd)
* 130 interrupted by the user (Ctrl+C)

The domain modules are reached only through deeppyro.cli.app, which boots the
kernel service graph; commands never import them directly.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from collections.abc import Callable
from typing import Any

import typer

from deeppyro.cli.app import (
    CliApp,
    await_maybe,
    build_app,
    list_session_infos,
)
from deeppyro.cli.chat import TurnInterrupts, chat_loop, interrupt_agent
from deeppyro.cli.frontend import (
    EXIT_ERROR,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    TerminalFrontend,
)
from deeppyro.config import load_config
from deeppyro.core.errors import ConfigError, DeeppyroError, SessionError

__version__ = "0.1.0"

app = typer.Typer(
    name="dpyro",
    help="DeepPyro Harness terminal client.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
config_app = typer.Typer(
    help="Inspect the effective configuration.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
app.add_typer(config_app, name="config")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"dpyro {__version__}")
    raise typer.Exit()


def _read_stdin_prompt() -> str:
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


def _install_sigint(interrupts: TurnInterrupts) -> Callable[[], None] | None:
    """Route Ctrl+C to the running turn instead of killing the process."""

    def handler(signum: int, frame: Any) -> None:
        interrupts.press()

    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, handler)
    except (ValueError, OSError):  # pragma: no cover - non-main thread / unsupported
        return None

    def restore() -> None:
        try:
            signal.signal(signal.SIGINT, previous)
        except (ValueError, OSError):  # pragma: no cover
            pass

    return restore


# --------------------------------------------------------------------------
# root callback
# --------------------------------------------------------------------------


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
) -> None:
    """DeepPyro Harness terminal client."""
    del version


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _run_impl(
    prompt: str | None,
    *,
    model: str | None,
    provider: str | None,
    json_output: bool,
    cwd: str | None,
    max_steps: int | None,
) -> int:
    frontend = TerminalFrontend()
    if max_steps is not None and max_steps < 1:
        frontend.error("error: --max-steps must be at least 1")
        return EXIT_USAGE
    text = prompt if prompt is not None else _read_stdin_prompt()
    if not text.strip():
        frontend.error("error: no prompt given (pass a prompt or pipe one on stdin)")
        return EXIT_USAGE
    try:
        return asyncio.run(
            _run_async(
                frontend,
                text,
                model=model,
                provider=provider,
                json_output=json_output,
                cwd=cwd,
                max_steps=max_steps,
            )
        )
    except KeyboardInterrupt:  # pragma: no cover - SIGINT outside an armed turn
        frontend.error("interrupted")
        return EXIT_INTERRUPTED


async def _run_async(
    frontend: TerminalFrontend,
    text: str,
    *,
    model: str | None,
    provider: str | None,
    json_output: bool,
    cwd: str | None,
    max_steps: int | None,
) -> int:
    try:
        cli_app = await await_maybe(build_app(provider=provider, model=model, cwd=cwd, frontend=frontend))
    except ConfigError as exc:
        frontend.error(f"error: {exc}")
        return EXIT_USAGE
    except DeeppyroError as exc:
        frontend.error(f"error: {exc}")
        return EXIT_ERROR

    sink = frontend.sink(json_mode=json_output)
    interrupts = TurnInterrupts()
    restore = _install_sigint(interrupts)
    try:
        session = cli_app.new_session()
        sink.emit({"type": "session", "sessionId": session.session_id})
        if not json_output:
            frontend.banner(
                cwd=str(cli_app.cwd),
                provider=cli_app.provider_name,
                model=cli_app.model,
                session_id=session.session_id,
            )
        agent = cli_app.create_agent(session, sink, max_steps=max_steps)
        sink.begin_turn()
        interrupted = False
        interrupts.arm(agent)
        try:
            await await_maybe(agent.send(text))
        except KeyboardInterrupt:
            await interrupt_agent(agent)
            interrupted = True
        except DeeppyroError as exc:
            sink.end_turn()
            frontend.error(f"error: {exc}")
            return EXIT_ERROR
        finally:
            interrupts.disarm()
        await interrupts.drain()
        sink.end_turn()
        if interrupted or interrupts.fired:
            frontend.error("interrupted")
            return EXIT_INTERRUPTED
        if not json_output:
            frontend.footer(sink)
        if getattr(sink, "saw_error", False):
            return EXIT_ERROR
        return EXIT_OK
    except (DeeppyroError, OSError) as exc:
        frontend.error(f"error: {exc}")
        return EXIT_ERROR
    finally:
        await await_maybe(cli_app.aclose())
        if restore is not None:
            restore()


@app.command()
def run(
    prompt: str | None = typer.Argument(None, help="Prompt text; omit to read stdin."),
    model: str | None = typer.Option(None, "--model", "-m", help="Override the model id."),
    provider: str | None = typer.Option(None, "--provider", "-p", help="Override the provider."),
    json_output: bool = typer.Option(False, "--json", help="Emit one NDJSON event per line."),
    cwd: str | None = typer.Option(None, "--cwd", help="Workspace directory."),
    max_steps: int | None = typer.Option(None, "--max-steps", min=1, help="Agent step budget."),
) -> None:
    """Run one non-interactive turn."""
    raise typer.Exit(
        _run_impl(
            prompt,
            model=model,
            provider=provider,
            json_output=json_output,
            cwd=cwd,
            max_steps=max_steps,
        )
    )


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------


def _chat_impl(*, resume: str | None, cwd: str | None, max_turns: int | None = None) -> int:
    frontend = TerminalFrontend()
    try:
        return asyncio.run(_chat_async(frontend, resume=resume, cwd=cwd, max_turns=max_turns))
    except KeyboardInterrupt:  # pragma: no cover - SIGINT outside an armed turn
        frontend.warning("interrupted")
        return EXIT_INTERRUPTED


async def _chat_async(
    frontend: TerminalFrontend,
    *,
    resume: str | None,
    cwd: str | None,
    max_turns: int | None,
) -> int:
    cli_app: CliApp | None = None
    try:
        cli_app = await await_maybe(build_app(provider=None, model=None, cwd=cwd, frontend=frontend))
    except ConfigError as exc:
        frontend.error(f"error: {exc}")
        return EXIT_USAGE
    except DeeppyroError as exc:
        frontend.error(f"error: {exc}")
        return EXIT_ERROR

    try:
        if resume:
            try:
                session = cli_app.open_session(resume)
            except (SessionError, KeyError):
                frontend.error(f"error: unknown session: {resume}")
                return EXIT_USAGE
        else:
            session = cli_app.new_session()
        sink = frontend.sink(json_mode=False, markdown_final=True)
        sink.emit({"type": "session", "sessionId": session.session_id})
        frontend.chat_header(
            cwd=str(cli_app.cwd),
            provider=cli_app.provider_name,
            model=cli_app.model,
            session_id=session.session_id,
            resumed=bool(resume),
        )
        interrupts = TurnInterrupts()
        restore = _install_sigint(interrupts)
        try:
            return await chat_loop(
                frontend,
                cli_app.store,
                session,
                lambda sess: cli_app.create_agent(sess, sink),
                sink=sink,
                new_session=cli_app.new_session,
                interrupt_requests=interrupts,
                max_turns=max_turns,
            )
        finally:
            if restore is not None:
                restore()
    except KeyboardInterrupt:
        frontend.warning("interrupted")
        return EXIT_INTERRUPTED
    except DeeppyroError as exc:
        frontend.error(f"error: {exc}")
        return EXIT_ERROR
    finally:
        await await_maybe(cli_app.aclose())


@app.command()
def chat(
    resume: str | None = typer.Option(None, "--resume", "-r", help="Resume a session id."),
    cwd: str | None = typer.Option(None, "--cwd", help="Workspace directory."),
) -> None:
    """Start the interactive terminal chat."""
    raise typer.Exit(_chat_impl(resume=resume, cwd=cwd))


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------


def _sessions_impl(*, limit: int) -> int:
    frontend = TerminalFrontend()
    try:
        cfg = load_config()
        infos = list_session_infos(cfg)
    except ConfigError as exc:
        frontend.error(f"error: {exc}")
        return EXIT_USAGE
    except (DeeppyroError, OSError) as exc:
        frontend.error(f"error: {exc}")
        return EXIT_ERROR
    frontend.session_table(infos, limit=limit)
    return EXIT_OK


@app.command()
def sessions(
    limit: int = typer.Option(20, "--limit", "-n", min=1, help="Maximum sessions to list."),
) -> None:
    """List recent local sessions."""
    raise typer.Exit(_sessions_impl(limit=limit))


# --------------------------------------------------------------------------
# config show
# --------------------------------------------------------------------------


def _config_show_impl(*, json_output: bool) -> int:
    frontend = TerminalFrontend()
    try:
        cfg = load_config()
        summary = frontend.config_summary(cfg)
    except ConfigError as exc:
        frontend.error(f"error: {exc}")
        return EXIT_USAGE
    except (DeeppyroError, OSError) as exc:
        frontend.error(f"error: {exc}")
        return EXIT_ERROR
    if json_output:
        frontend.print_json(summary)
    else:
        frontend.config(summary)
    return EXIT_OK


@config_app.command("show")
def config_show(
    json_output: bool = typer.Option(False, "--json", help="Emit the config as one JSON document."),
) -> None:
    """Show the effective configuration with secrets masked."""
    raise typer.Exit(_config_show_impl(json_output=json_output))


def main() -> None:
    """Console-script entry point (project.scripts dpyro)."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
