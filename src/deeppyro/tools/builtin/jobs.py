"""Agent-facing background job tools: job_output, job_kill and job_list.

The tools resolve the jobs service from ToolContext.services; the kernel jobs
plugin registers them and the runtime decides whether a session exposes the
capability. Job output is bounded to a head/tail preview, while the complete
output stays on the live handle.
"""

from __future__ import annotations

from typing import Any

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.args import optional_bool, optional_int, optional_str, require_str
from deeppyro.tools.base import BaseTool, make_parameters
from deeppyro.tools.spill import truncate_text

#: Default and maximum wait for job_output(wait=true), in milliseconds.
DEFAULT_WAIT_MS = 30_000
MAX_WAIT_MS = 600_000

#: Inline preview budget for one job_output call (head and tail kept).
OUTPUT_PREVIEW_CHARS = 4_000


def _service(ctx: ToolContext) -> Any:
    service = (ctx.services or {}).get("jobs")
    if service is None:
        raise ToolError("jobs service is not available in this session")
    return service


def _handle(service: Any, job_id: str) -> Any:
    handle = service.get(job_id)
    if handle is None:
        raise ToolError(f"unknown job: {job_id}")
    return handle


def _timeout_ms(args: dict[str, Any]) -> int:
    """Read timeout_ms, accepting the camelCase spelling for tolerance."""
    if "timeout_ms" not in args and "timeoutMs" in args:
        args = dict(args)
        args["timeout_ms"] = args.pop("timeoutMs")
    return optional_int(args, "timeout_ms", DEFAULT_WAIT_MS, minimum=1, maximum=MAX_WAIT_MS)


def _status_line(status: str, exit_code: int | None) -> str:
    if status == "completed" and exit_code is not None:
        return f"[status: {status}, exit code: {exit_code}]"
    return f"[status: {status}]"


def _preview(output: str) -> tuple[str, int]:
    return truncate_text(output, OUTPUT_PREVIEW_CHARS)


class JobOutputTool(BaseTool):
    """Read accumulated output and the current status of one background job."""

    name = "job_output"
    description = (
        "Read a background job: output since the previous read for stream jobs, or the result "
        "of a finished final-output job."
    )
    parameters = make_parameters(
        {
            "job_id": {
                "type": "string",
                "description": "Job id returned by the tool that started the background work.",
            },
            "wait": {
                "type": "boolean",
                "description": (
                    "Block until the job finishes or the timeout expires; a timed-out wait "
                    "leaves the job running. Defaults to false."
                ),
            },
            "timeout_ms": {
                "type": "number",
                "description": (
                    "Max wait in milliseconds with wait: true. Defaults to and is capped by configuration."
                ),
            },
        },
        required=["job_id"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        job_id = require_str(args, "job_id")
        handle = _handle(service, job_id)
        wait = optional_bool(args, "wait", False)
        timeout_ms = _timeout_ms(args)
        timed_out = False
        if wait and handle.running:
            result = await handle.wait(timeout_ms / 1000)
            timed_out = result.timed_out
        else:
            result = handle.snapshot()
        preview, omitted = _preview(result.output)
        body = preview if preview else "(no new output)"
        text = f"{body}\n{_status_line(result.status, result.exit_code)}"
        meta: dict[str, Any] = {
            "jobId": result.id,
            "status": result.status,
            "exitCode": result.exit_code,
            "outputChars": len(result.output),
            "truncated": omitted > 0,
            "omittedChars": omitted,
            "waitTimedOut": timed_out,
        }
        if result.detail:
            meta["detail"] = result.detail
        return ToolResult(text=text, meta=meta)


class JobKillTool(BaseTool):
    """Request cancellation of one background job."""

    name = "job_kill"
    description = "Request cancellation of a running background job."
    parameters = make_parameters(
        {
            "job_id": {
                "type": "string",
                "description": "Job id returned by the tool that started the background work.",
            },
            "reason": {
                "type": "string",
                "description": "Optional short reason, recorded in the log and forwarded to the job.",
            },
        },
        required=["job_id"],
    )

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        job_id = require_str(args, "job_id")
        handle = _handle(service, job_id)
        reason = optional_str(args, "reason")
        if handle.running:
            await service.kill(job_id, reason)
        current = service.get(job_id) or handle
        text = f"kill requested for {job_id}\n{_status_line(current.status, current.exit_code)}"
        meta: dict[str, Any] = {
            "jobId": job_id,
            "status": current.status,
            "exitCode": current.exit_code,
        }
        if reason:
            meta["reason"] = reason
        return ToolResult(text=text, meta=meta)


class JobListTool(BaseTool):
    """List every background job known to the session."""

    name = "job_list"
    description = "List your background jobs (running and finished) with their ids, kinds, and statuses."
    parameters = make_parameters({})

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = _service(ctx)
        handles = service.list()
        if not handles:
            return ToolResult(text="No background jobs.", meta={"count": 0})
        lines = []
        for handle in handles:
            label = " ".join(str(handle.label).split())
            lines.append(f"{handle.id} [{handle.kind}] {handle.status} \u2014 {label}")
        return ToolResult(text="\n".join(lines), meta={"count": len(handles)})


def job_tools() -> list[BaseTool]:
    """The background job control tool set registered by the jobs plugin."""
    return [JobOutputTool(), JobKillTool(), JobListTool()]


__all__ = [
    "DEFAULT_WAIT_MS",
    "MAX_WAIT_MS",
    "OUTPUT_PREVIEW_CHARS",
    "JobKillTool",
    "JobListTool",
    "JobOutputTool",
    "job_tools",
]
