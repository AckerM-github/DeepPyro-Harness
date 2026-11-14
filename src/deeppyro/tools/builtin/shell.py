"""Shell tools: pwsh (Windows) and bash.

Both run one fresh process per call, merge stderr into stdout, and honour
timeoutMs (default 120000, capped at 600000). A non-zero exit code is appended
to the text as "[exit code: N]" but is NOT an error result - the model decides
whether the failure matters, matching the DSH fixtures.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from pathlib import Path
from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.args import optional_bool, optional_int, optional_str, require_str
from deeppyro.tools.base import BaseTool, make_parameters, sandbox_properties
from deeppyro.tools.paths import resolve_path
from deeppyro.tools.permissions import check_command

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000


class _ShellTool(BaseTool):
    """Shared execution logic for the shell tools."""

    parallel_safe = False

    def argv(self, command: str) -> list[str]:  # pragma: no cover - overridden
        raise NotImplementedError

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
    ) -> ToolResult:
        """Admit a real background job and return its id immediately."""
        jobs = (ctx.services or {}).get("jobs")
        if jobs is None:
            raise ToolError(
                "run_in_background requires the jobs service, which is not available in this session"
            )

        async def runner(handle: Any) -> int | None:
            process = await self._spawn(argv, cwd)
            try:
                assert process.stdout is not None
                while True:
                    chunk = await process.stdout.read(65536)
                    if not chunk:
                        break
                    handle.write(chunk.decode("utf-8", errors="replace").replace("\r\n", "\n"))
                return await process.wait()
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
                raise

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
            meta={"jobId": handle.id, "kind": self.name},
        )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = require_str(args, "command")
        timeout_ms = optional_int(args, "timeoutMs", DEFAULT_TIMEOUT_MS, minimum=1, maximum=MAX_TIMEOUT_MS)
        background = optional_bool(args, "run_in_background", False)
        workdir_arg = optional_str(args, "workdir")
        await check_command(ctx, args, command, tool=self.name)
        cwd = resolve_path(Path(ctx.cwd), workdir_arg) if workdir_arg else Path(ctx.cwd).resolve(strict=False)
        if not cwd.is_dir():
            raise ToolError(f"workdir is not a directory: {cwd}")
        argv = self.argv(command)
        if background:
            return await self._run_background(command, argv, cwd, ctx)
        started = time.monotonic()
        process = await self._spawn(argv, cwd)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_ms / 1000)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(Exception):
                await process.wait()
            raise ToolError(f"command timed out after {timeout_ms}ms and was killed: {command}") from None
        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = process.returncode or 0
        text = (stdout or b"").decode("utf-8", errors="replace").replace("\r\n", "\n").rstrip("\n")
        if exit_code != 0:
            text = f"{text}\n\n[exit code: {exit_code}]" if text else f"[exit code: {exit_code}]"
        return ToolResult(
            text=text, meta={"exitCode": exit_code, "durationMs": duration_ms, "workdir": str(cwd)}
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
    parameters = make_parameters(
        sandbox_properties(
            {
                "description": {
                    "type": "string",
                    "description": "Clear, concise description of what this command does in active voice, 5-10 words.",
                },
                "command": {"type": "string", "description": "The PowerShell command to execute."},
                "timeoutMs": {
                    "type": "number",
                    "description": "Timeout in milliseconds (default 120000, max 600000).",
                },
                "workdir": {"type": "string", "description": "Working directory for this command."},
                "run_in_background": {
                    "type": "boolean",
                    "description": "Start a background job and return its job id immediately.",
                },
            }
        ),
        required=["description", "command"],
    )

    def argv(self, command: str) -> list[str]:
        # Prefer PowerShell 7; fall back to Windows PowerShell so the tool works
        # on hosts that only ship the in-box engine.
        binary = shutil.which("pwsh") or shutil.which("powershell") or "pwsh"
        return [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        require_str(args, "description")
        return await super().run(args, ctx)


class BashTool(_ShellTool):
    """Run a bash command in a fresh shell process."""

    name = "bash"
    description = (
        "Run a bash command and return its stdout/stderr. Each call runs in a fresh bash process; pass workdir "
        "instead of using cd. A non-zero exit code is appended as [exit code: N]."
    )
    parameters = make_parameters(
        {
            "command": {"type": "string", "description": "The bash command to execute."},
            "timeoutMs": {
                "type": "number",
                "description": "Timeout in milliseconds (default 120000, max 600000).",
            },
            "workdir": {"type": "string", "description": "Working directory for this command."},
            "run_in_background": {
                "type": "boolean",
                "description": "Start a background job and return its job id immediately.",
            },
        },
        required=["command"],
    )

    def argv(self, command: str) -> list[str]:
        binary = shutil.which("bash") or shutil.which("sh") or "bash"
        return [binary, "-lc", command]


__all__ = ["DEFAULT_TIMEOUT_MS", "MAX_TIMEOUT_MS", "BashTool", "PwshTool"]
