"""Shell tools: pwsh (Windows) and bash.

Both run one fresh process per call, merge stderr into stdout, and honour
timeoutMs (default 120000, capped at 600000). A non-zero exit code is appended
to the text as "[exit code: N]" but is NOT an error result - the model decides
whether the failure matters, matching the upstream fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.sandbox.policy import SandboxPolicy
from deeppyro.sandbox.provider import (
    WIDER_MODES,
    RunnerFailureRule,
    SandboxUnavailableError,
    classify_runner_failure,
    escalation_hint_marker,
    matches_denial,
    sandbox_denial_marker,
)
from deeppyro.sandbox.types import Preset
from deeppyro.tools.base import (
    JUSTIFICATION_DESCRIPTION,
    SANDBOX_MODES,
    SANDBOX_PERMISSIONS_DESCRIPTION,
    BaseTool,
)
from deeppyro.tools.params import NonEmptyStr, OptionalNonEmptyStr, ToolArgs, schema_enum, schema_number
from deeppyro.tools.paths import resolve_path
from deeppyro.tools.permissions import check_command, gate_for

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000


class _ShellTool(BaseTool):
    """Shared execution logic for the shell tools."""

    parallel_safe = False

    def argv(self, command: str) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def _confine(
        self, argv: list[str], ctx: ToolContext, mode: Preset
    ) -> tuple[list[str], str, tuple[str, ...], tuple[RunnerFailureRule, ...], Path | None]:
        """Run the command through the sandbox provider; fail closed."""
        sandbox = (ctx.services or {}).get("sandbox")
        gate = gate_for(ctx)
        policy = gate.policy
        if mode != policy.mode:
            policy = SandboxPolicy(
                mode=mode,
                workspace_root=policy.workspace_root,
                additional_writable_roots=policy.additional_writable_roots,
            )
        if sandbox is None or not callable(getattr(sandbox, "confine", None)):
            return list(argv), "none", (), (), None
        try:
            confinement = sandbox.confine(argv, policy, signal=ctx.cancel)
        except SandboxUnavailableError as exc:
            raise ToolError(str(exc)) from exc
        except Exception as exc:
            raise ToolError(f"cannot confine the command: {exc}") from exc
        return (
            list(confinement.argv),
            str(getattr(confinement, "enforcement", "none")),
            tuple(getattr(confinement, "denial_signatures", ()) or ()),
            tuple(getattr(confinement, "runner_failure_rules", ()) or ()),
            getattr(confinement, "temp_root", None),
        )

    @staticmethod
    def _sandbox_note(
        text: str,
        mode: Preset,
        signatures: tuple[str, ...],
        rules: tuple[RunnerFailureRule, ...],
        exit_code: int | None,
    ) -> tuple[str, bool, bool]:
        """Append the upstream sandbox notice; return (text, denied, runner_failed)."""
        runner_failed = classify_runner_failure(exit_code, text, rules) is not None
        if runner_failed:
            notice = (
                f"[sandbox: the sandbox runner itself failed under {mode} mode — the command did not run; "
                "this is a sandbox problem, not a command failure]"
            )
            return f"{text}\n\n{notice}" if text else notice, False, True
        if not text or not signatures or not matches_denial(text, signatures):
            return text, False, False
        notice = sandbox_denial_marker(mode)
        if mode in WIDER_MODES:
            notice += "\n" + escalation_hint_marker("command")
        return f"{text}\n\n{notice}", True, False

    @staticmethod
    def _cleanup_temp(temp_root: Path | None) -> None:
        """Remove the private per-call temp root the runner was given."""
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)

    async def _spawn(self, argv: list[str], cwd: Path) -> asyncio.subprocess.Process:
        """Start one shell process with stderr merged into stdout."""
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            raise ToolError(f"cannot start shell: {argv[0]!r} was not found on PATH") from None
        except OSError as exc:
            raise ToolError(f"cannot start shell {argv[0]!r}: {exc}") from exc

    async def _run_background(
        self,
        command: str,
        argv: list[str],
        cwd: Path,
        ctx: ToolContext,
        *,
        mode: Preset,
        enforcement: str,
        denial_signatures: tuple[str, ...],
        runner_rules: tuple[RunnerFailureRule, ...],
        temp_root: Path | None,
    ) -> ToolResult:
        """Admit a real background job and return its id immediately."""
        jobs = (ctx.services or {}).get("jobs")
        if jobs is None:
            raise ToolError(
                "run_in_background requires the jobs service, which is not available in this session"
            )

        async def runner(handle: Any) -> int | None:
            chunks: list[str] = []
            process = await self._spawn(argv, cwd)
            try:
                assert process.stdout is not None
                while True:
                    chunk = await process.stdout.read(65536)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
                    chunks.append(text)
                    handle.write(text)
                exit_code = await process.wait()
                annotated, _denied, _failed = self._sandbox_note(
                    "".join(chunks).rstrip("\n"), mode, denial_signatures, runner_rules, exit_code
                )
                if annotated != "".join(chunks).rstrip("\n"):
                    handle.write(annotated[len("".join(chunks).rstrip("\n")) :] + "\n")
                return exit_code
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
                raise
            finally:
                self._cleanup_temp(temp_root)

        handle = jobs.start(
            self.name,
            command,
            runner,
            owner=ctx.session_id,
            call_id=ctx.call_id,
            record=getattr(ctx, "log", None),
        )
        return ToolResult(
            text=f"started background job {handle.id}",
            meta={
                "jobId": handle.id,
                "kind": self.name,
                "sandbox": {"mode": mode, "enforcement": enforcement},
            },
        )

    def _parse(self, args: dict[str, Any]) -> Any:
        """Validate args against this tool's nested Args model."""
        model = type(self).Args
        if model is None:  # pragma: no cover - both shell tools declare Args
            raise ToolError(f"{self.name} has no argument model")
        return model.parse(args)

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        parsed = self._parse(args)
        command = parsed.command
        timeout_ms = parsed.timeoutMs if parsed.timeoutMs is not None else DEFAULT_TIMEOUT_MS
        background = bool(parsed.run_in_background)
        workdir_arg = parsed.workdir
        mode = await check_command(ctx, args, command, tool=self.name)
        cwd = resolve_path(Path(ctx.cwd), workdir_arg) if workdir_arg else Path(ctx.cwd).resolve(strict=False)
        if not cwd.is_dir():
            raise ToolError(f"workdir is not a directory: {cwd}")
        argv, enforcement, denial_signatures, runner_rules, temp_root = self._confine(
            self.argv(command), ctx, mode
        )
        if background:
            return await self._run_background(
                command,
                argv,
                cwd,
                ctx,
                mode=mode,
                enforcement=enforcement,
                denial_signatures=denial_signatures,
                runner_rules=runner_rules,
                temp_root=temp_root,
            )
        started = time.monotonic()
        try:
            process = await self._spawn(argv, cwd)
        except BaseException:
            self._cleanup_temp(temp_root)
            raise
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise ToolError(f"command timed out after {timeout_ms}ms and was killed: {command}") from None
        finally:
            self._cleanup_temp(temp_root)
        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = process.returncode or 0
        text = (stdout or b"").decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")
        if exit_code != 0:
            text = f"{text}\n\n[exit code: {exit_code}]" if text else f"[exit code: {exit_code}]"
        text, denied, runner_failed = self._sandbox_note(
            text, mode, denial_signatures, runner_rules, exit_code
        )
        facts: dict[str, Any] = {"mode": mode, "enforcement": enforcement, "denied": denied}
        if runner_failed:
            facts["runnerFailed"] = True
        return ToolResult(
            text=text,
            meta={
                "exitCode": exit_code,
                "durationMs": duration_ms,
                "workdir": str(cwd),
                "sandbox": facts,
            },
        )


class PwshTool(_ShellTool):
    """Run a PowerShell command in a fresh pwsh process."""

    name = "pwsh"
    description = (
        "Execute a PowerShell command (`pwsh -Command`) and return its stdout/stderr. Each call runs in a "
        "fresh pwsh process; pass `workdir` instead of using `cd`. Paths use native Windows form "
        "(`C:\\...`); read environment variables with `$env:NAME`. Long output is truncated to its "
        "tail; the full output is saved to a file whose path is reported when available. A force-killed command "
        "settles as `[exit code: N]` without a signal marker - treat that as an interruption, not a command "
        "failure. Provide `description` before `command` in the arguments."
    )

    class Args(ToolArgs):
        description: NonEmptyStr = Field(
            description="Clear, concise description of what this command does in active voice, 5-10 words.",
        )
        command: NonEmptyStr = Field(description="The PowerShell command to execute.")
        timeoutMs: int | None = Field(  # noqa: N815
            default=None,
            ge=1,
            le=MAX_TIMEOUT_MS,
            json_schema_extra=schema_number(),
            description="Timeout in milliseconds (default 120000, max 600000).",
        )
        workdir: OptionalNonEmptyStr = Field(
            default=None,
            description="Working directory for this command.",
        )
        run_in_background: bool | None = Field(
            default=None,
            description="Start a background job and return its job id immediately.",
        )
        sandbox_permissions: str | None = Field(
            default=None,
            json_schema_extra=schema_enum(*SANDBOX_MODES),
            description=SANDBOX_PERMISSIONS_DESCRIPTION,
        )
        justification: str | None = Field(default=None, description=JUSTIFICATION_DESCRIPTION)

    def argv(self, command: str) -> list[str]:
        # Prefer PowerShell 7; fall back to Windows PowerShell so the tool works
        # on hosts that only ship the in-box engine.
        binary = shutil.which("pwsh") or shutil.which("powershell") or "pwsh"
        return [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]


class BashTool(_ShellTool):
    """Run a bash command in a fresh shell process."""

    name = "bash"
    description = (
        "Run a bash command and return its stdout/stderr. Each call runs in a fresh bash process; pass workdir "
        "instead of using cd. A non-zero exit code is appended as [exit code: N]."
    )

    class Args(ToolArgs):
        command: NonEmptyStr = Field(description="The bash command to execute.")
        timeoutMs: int | None = Field(  # noqa: N815
            default=None,
            ge=1,
            le=MAX_TIMEOUT_MS,
            json_schema_extra=schema_number(),
            description="Timeout in milliseconds (default 120000, max 600000).",
        )
        workdir: OptionalNonEmptyStr = Field(
            default=None,
            description="Working directory for this command.",
        )
        run_in_background: bool | None = Field(
            default=None,
            description="Start a background job and return its job id immediately.",
        )

    def argv(self, command: str) -> list[str]:
        binary = shutil.which("bash") or shutil.which("sh") or "bash"
        return [binary, "-lc", command]


__all__ = ["DEFAULT_TIMEOUT_MS", "MAX_TIMEOUT_MS", "BashTool", "PwshTool"]
