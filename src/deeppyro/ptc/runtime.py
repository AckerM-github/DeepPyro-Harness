"""Per-call Python PTC runtime.

Each invocation starts a fresh CPython subprocess running deeppyro.ptc.worker.
The worker speaks a line-delimited JSON protocol on stdin/stdout; program print
output is forwarded as log frames.  Tool bindings are dispatched on the host
through the callables supplied by the run_code tool.

This is deliberately a very small implementation of the DSH PTC contract.  It
is not an operating-system sandbox: the subprocess can read and write whatever
the current user can.  The PermissionGate in front of run_code is the only
policy boundary.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, ClassVar

from deeppyro.core.errors import ConfigError
from deeppyro.core.protocols import ToolContext

from .types import (
    PTC_FAILURE_KINDS,
    PtcBindingError,
    PtcConfig,
    PtcFailure,
    PtcRunResult,
    PtcRunSpec,
)

Binding = Callable[[dict[str, Any]], Awaitable[Any] | Any]
WORKER_PATH = Path(__file__).with_name("worker.py")
_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
)


class _RunState:
    """Mutable state shared by the stdout/stderr readers and binding tasks."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        max_output_bytes: int,
        max_pending_calls: int,
        stdin: asyncio.StreamWriter,
    ) -> None:
        self.result: asyncio.Future[PtcRunResult] = loop.create_future()
        self.logs: list[str] = []
        self.log_bytes = 0
        self.max_output_bytes = max_output_bytes
        self.semaphore = asyncio.Semaphore(max_pending_calls)
        self.stdin = stdin
        self.send_lock = asyncio.Lock()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.output_exceeded = False

    def add_log(self, text: str) -> bool:
        if not text:
            return True
        size = len(text.encode("utf-8", "replace"))
        if self.log_bytes + size > self.max_output_bytes:
            self.output_exceeded = True
            return False
        self.logs.append(text)
        self.log_bytes += size
        return True

    def settle(self, result: PtcRunResult) -> None:
        if not self.result.done():
            self.result.set_result(result)

    async def send(self, frame: Mapping[str, Any]) -> None:
        line = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        async with self.send_lock:
            self.stdin.write(line.encode("utf-8") + b"\n")
            await self.stdin.drain()

    def spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)


class PtcRuntime:
    """The PTC service exposed as the "ptc" service.

    The class is intentionally independent of the plugin context so it can be
    unit tested directly.  PtcPlugin constructs it and publishes it through
    ctx.provide("ptc", runtime).
    """

    provide = "ptc"
    inject: ClassVar[list[str]] = ["tools", "sandbox"]

    def __init__(
        self,
        config: PtcConfig | Mapping[str, Any] | None = None,
        *,
        worker_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.config = config if isinstance(config, PtcConfig) else PtcConfig.from_mapping(config)
        self.worker_path = Path(worker_path) if worker_path is not None else WORKER_PATH

    @property
    def timeout(self) -> dict[str, int]:
        """The DSH-shaped timeout descriptor."""
        return self.config.timeout

    @property
    def mode(self) -> str:
        """The configured presentation mode: "native" or "ptc"."""
        return self.config.mode

    @property
    def language(self) -> str:
        """The PTC program language."""
        return "python"

    @property
    def program_format(self) -> str:
        """The format of the program source accepted by run_code."""
        return "python"

    @property
    def sandbox_mode(self) -> None:
        """PTC has no OS-level sandbox policy of its own."""
        return None

    @property
    def execution_instructions(self) -> str:
        """Short execution-model note appended to the run_code description."""
        return (
            "Programs run in a fresh CPython process started with -I -S. The working directory is "
            "fixed to the Session's current directory. Inside a program only tools and ToolCallError "
            "are bound; call a tool as await tools.name(args)."
        )

    def resolve(self, request: Mapping[str, Any] | PtcRunSpec, *, cwd: Path | None = None) -> PtcRunSpec:
        """Build a run spec from model arguments.

        The tool path normally builds PtcRunSpec directly; resolve() is also
        useful for embedders and tests.  A timeout above maxTimeoutMs is capped.
        """
        if isinstance(request, PtcRunSpec):
            return request
        if not isinstance(request, Mapping):
            raise ConfigError("ptc: run request must be a table")
        program = request.get("program")
        if not isinstance(program, str):
            raise ConfigError("ptc: program must be a string")
        raw_timeout = request.get("timeoutMs", request.get("timeout_ms", self.config.timeout_ms))
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int) or raw_timeout <= 0:
            raise ConfigError("ptc: timeoutMs must be a positive integer")
        target_cwd = request.get("cwd", cwd)
        if target_cwd is None:
            target_cwd = Path.cwd()
        raw_max = request.get("maxOutputBytes", request.get("max_output_bytes", self.config.max_output_bytes))
        if isinstance(raw_max, bool) or not isinstance(raw_max, int) or raw_max <= 0:
            raise ConfigError("ptc: maxOutputBytes must be a positive integer")
        return PtcRunSpec(
            program=program,
            cwd=Path(target_cwd),
            timeout_ms=min(raw_timeout, self.config.max_timeout_ms),
            max_output_bytes=raw_max,
            sandbox_mode=str(request.get("sandboxMode", request.get("sandbox_mode", "workspace-write"))),
        )

    async def run(
        self,
        spec: PtcRunSpec,
        *,
        bindings: Mapping[str, Binding] | None,
        ctx: ToolContext,
    ) -> PtcRunResult:
        """Execute one program and return a structured result.

        Program failures are returned in PtcRunResult.failure; only host-level
        programming errors (a missing worker script) are also reported that way.
        Task cancellation is re-raised after the subprocess has been stopped.
        """
        if not isinstance(spec, PtcRunSpec):
            raise TypeError("spec must be a PtcRunSpec")
        if ctx.cancel.is_set():
            return self._result((), PtcFailure("abort", "run_code was aborted"))
        if not self.worker_path.is_file():
            return self._result(
                (), PtcFailure("worker-exit", f"the PTC worker script is missing: {self.worker_path}")
            )
        timeout_ms = max(1, min(int(spec.timeout_ms), self.config.max_timeout_ms))
        env = self._worker_env()
        argv = [
            sys.executable,
            "-I",
            "-S",
            "-X",
            "utf8",
            str(self.worker_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(spec.cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=max(1024 * 1024, min(spec.max_output_bytes + 1024 * 1024, 128 * 1024 * 1024)),
            )
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            return self._result((), PtcFailure("worker-exit", f"cannot start the Python PTC worker: {exc}"))
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        state = _RunState(
            asyncio.get_running_loop(),
            max_output_bytes=spec.max_output_bytes,
            max_pending_calls=self.config.max_pending_calls,
            stdin=process.stdin,
        )
        binding_map: dict[str, Binding] = dict(bindings or {})
        stdout_task = asyncio.create_task(self._read_stdout(process, state, binding_map))
        stderr_task = asyncio.create_task(self._read_stderr(process, state))
        cancel_task = asyncio.create_task(ctx.cancel.wait())
        try:
            request_frame = {
                "protocol": 1,
                "program": spec.program,
                "bindings": list(binding_map),
            }
            try:
                await state.send(request_frame)
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                await self._terminate(process, self.config.grace_ms)
                return self._result(
                    tuple(state.logs),
                    PtcFailure("worker-exit", f"cannot send the program to the worker: {exc}"),
                )
            done, _pending = await asyncio.wait(
                {state.result, cancel_task},
                timeout=timeout_ms / 1000.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if state.result in done:
                return state.result.result()
            if cancel_task in done:
                await self._terminate(process, self.config.grace_ms)
                await self._drain(state, stdout_task, stderr_task, cancel_task)
                return self._result(tuple(state.logs), PtcFailure("abort", "run_code was aborted"))
            await self._terminate(process, self.config.grace_ms)
            await self._drain(state, stdout_task, stderr_task, cancel_task)
            message = f"run_code timed out after {timeout_ms}ms"
            return self._result(tuple(state.logs), PtcFailure("timeout", message))
        finally:
            for task in (stdout_task, stderr_task, cancel_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdout_task, stderr_task, cancel_task, return_exceptions=True)
            for task in list(state.tasks):
                task.cancel()
            if state.tasks:
                await asyncio.gather(*state.tasks, return_exceptions=True)
            if process.returncode is None:
                await self._terminate(process, self.config.grace_ms)
            self._close_transports(process)

    @staticmethod
    def _close_transports(process: asyncio.subprocess.Process) -> None:
        stdin = process.stdin
        if stdin is not None:
            try:
                stdin.close()
            except Exception:  # noqa: BLE001, S110 - best-effort pipe cleanup
                pass
        for stream in (process.stdout, process.stderr):
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception:  # noqa: BLE001, S110 - best-effort pipe cleanup
                    pass

    @staticmethod
    def _result(logs: tuple[str, ...], failure: PtcFailure | None, value: Any = None) -> PtcRunResult:
        return PtcRunResult(
            value=value,
            logs=logs,
            failure=failure,
            sandbox={"mode": "workspace-write", "enforcement": "none", "denial": "none"},
        )

    @staticmethod
    def _worker_env() -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if key in _ENV_KEYS}
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUTF8"] = "1"
        return env

    async def _read_stdout(
        self,
        process: asyncio.subprocess.Process,
        state: _RunState,
        bindings: Mapping[str, Binding],
    ) -> None:
        assert process.stdout is not None
        while True:
            try:
                line = await process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                state.settle(
                    self._result(
                        tuple(state.logs), PtcFailure("protocol", f"protocol message is too large: {exc}")
                    )
                )
                return
            except asyncio.CancelledError:
                raise
            if not line:
                code = await process.wait()
                state.settle(
                    self._result(
                        tuple(state.logs),
                        PtcFailure(
                            "worker-exit", f"the PTC worker exited with code {code} before completing"
                        ),
                    )
                )
                return
            try:
                frame = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                state.settle(
                    self._result(tuple(state.logs), PtcFailure("protocol", f"invalid protocol frame: {exc}"))
                )
                return
            if not isinstance(frame, dict):
                state.settle(
                    self._result(tuple(state.logs), PtcFailure("protocol", "protocol frame is not an object"))
                )
                return
            kind = frame.get("kind")
            if kind == "log":
                text = frame.get("text")
                if not isinstance(text, str):
                    state.settle(
                        self._result(tuple(state.logs), PtcFailure("protocol", "log frame has no text"))
                    )
                    return
                if not state.add_log(text):
                    state.settle(
                        self._result(
                            tuple(state.logs),
                            PtcFailure(
                                "output-limit",
                                f"run_code output exceeded the {state.max_output_bytes} byte limit",
                            ),
                        )
                    )
                    return
            elif kind == "call":
                state.spawn(self._handle_call(frame, state, bindings))
            elif kind == "done":
                state.settle(self._success_result(frame, state))
                return
            elif kind == "error":
                failure_kind = frame.get("errorKind")
                if failure_kind not in PTC_FAILURE_KINDS:
                    failure_kind = "protocol"
                message = frame.get("message")
                if not isinstance(message, str):
                    message = "the PTC program failed"
                state.settle(self._result(tuple(state.logs), PtcFailure(failure_kind, message)))
                return
            else:
                state.settle(
                    self._result(
                        tuple(state.logs), PtcFailure("protocol", f"unknown protocol frame kind: {kind!r}")
                    )
                )
                return

    def _success_result(self, frame: Mapping[str, Any], state: _RunState) -> PtcRunResult:
        if state.output_exceeded:
            return self._result(
                tuple(state.logs),
                PtcFailure(
                    "output-limit", f"run_code output exceeded the {state.max_output_bytes} byte limit"
                ),
            )
        value = frame.get("value")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            return self._result(
                tuple(state.logs), PtcFailure("invalid-output", f"the program returned invalid JSON: {exc}")
            )
        total = state.log_bytes + len(encoded.encode("utf-8"))
        if total > state.max_output_bytes:
            return self._result(
                tuple(state.logs),
                PtcFailure(
                    "output-limit", f"run_code output exceeded the {state.max_output_bytes} byte limit"
                ),
            )
        return self._result(tuple(state.logs), None, value=value)

    async def _read_stderr(self, process: asyncio.subprocess.Process, state: _RunState) -> None:
        assert process.stderr is not None
        while True:
            try:
                chunk = await process.stderr.read(65536)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - stderr draining must never fail the run
                return
            if not chunk:
                return
            text = chunk.decode("utf-8", "replace")
            if not state.add_log(text):
                state.settle(
                    self._result(
                        tuple(state.logs),
                        PtcFailure(
                            "output-limit",
                            f"run_code output exceeded the {state.max_output_bytes} byte limit",
                        ),
                    )
                )
                return

    async def _handle_call(
        self,
        frame: Mapping[str, Any],
        state: _RunState,
        bindings: Mapping[str, Binding],
    ) -> None:
        call_id = frame.get("id")
        name = frame.get("name")
        arguments = frame.get("arguments", {})
        if isinstance(call_id, bool) or not isinstance(call_id, int):
            state.settle(
                self._result(tuple(state.logs), PtcFailure("protocol", "call frame has no integer id"))
            )
            return
        if not isinstance(name, str) or not isinstance(arguments, dict):
            await self._send_safely(
                state,
                {
                    "kind": "result",
                    "id": call_id,
                    "ok": False,
                    "error": {"name": str(name), "code": "PROTOCOL", "reason": "invalid call frame"},
                },
            )
            return
        async with state.semaphore:
            if state.result.done():
                return
            binding = bindings.get(name)
            if binding is None:
                await self._send_safely(
                    state,
                    {
                        "kind": "result",
                        "id": call_id,
                        "ok": False,
                        "error": {"name": name, "code": "UNKNOWN_TOOL", "reason": f'unknown tool "{name}"'},
                    },
                )
                return
            try:
                outcome = binding(arguments)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            except PtcBindingError as exc:
                await self._send_safely(
                    state,
                    {"kind": "result", "id": call_id, "ok": False, "error": exc.to_dict()},
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - tool failures become structured binding errors
                await self._send_safely(
                    state,
                    {
                        "kind": "result",
                        "id": call_id,
                        "ok": False,
                        "error": {"name": name, "code": "TOOL_ERROR", "reason": str(exc)},
                    },
                )
                return
            await self._send_safely(state, {"kind": "result", "id": call_id, "ok": True, "value": outcome})

    async def _send_safely(self, state: _RunState, frame: Mapping[str, Any]) -> None:
        try:
            await state.send(frame)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    async def _drain(
        self,
        state: _RunState,
        stdout_task: asyncio.Task[Any],
        stderr_task: asyncio.Task[Any],
        cancel_task: asyncio.Task[Any],
    ) -> None:
        for task in (stdout_task, stderr_task, cancel_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, cancel_task, return_exceptions=True)
        for task in list(state.tasks):
            task.cancel()
        if state.tasks:
            await asyncio.gather(*state.tasks, return_exceptions=True)

    async def _terminate(self, process: asyncio.subprocess.Process, grace_ms: int | None = None) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            return
        wait_s = max(0.0, (self.config.grace_ms if grace_ms is None else grace_ms) / 1000.0)
        try:
            await asyncio.wait_for(process.wait(), timeout=wait_s)
        except TimeoutError:
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                return
            await process.wait()


PtcService = PtcRuntime


__all__ = ["WORKER_PATH", "Binding", "PtcRuntime", "PtcService"]
