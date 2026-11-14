"""Runtime tests for the Python PTC worker protocol."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from deeppyro.core.errors import ConfigError
from deeppyro.ptc import (
    PtcBindingError,
    PtcConfig,
    PtcFailure,
    PtcRunResult,
    PtcRunSpec,
    PtcRuntime,
)
from deeppyro.tools.registry import ToolRegistry

from .helpers import make_context


def run_spec(
    program: str, cwd: Path, *, timeout_ms: int = 10_000, max_output_bytes: int = 1_000_000
) -> PtcRunSpec:
    return PtcRunSpec(
        program=program,
        cwd=cwd,
        timeout_ms=timeout_ms,
        max_output_bytes=max_output_bytes,
        sandbox_mode="workspace-write",
    )


async def test_run_returns_json_value(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    ctx = make_context(tmp_path)
    result = await runtime.run(run_spec("return {'answer': 42}", tmp_path), bindings=None, ctx=ctx)
    assert result.ok
    assert result.failure is None
    assert result.value == {"answer": 42}
    assert result.sandbox == {"mode": "workspace-write", "enforcement": "none", "denial": "none"}


async def test_print_output_becomes_logs(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    result = await runtime.run(
        run_spec("print('hello'); print('world'); return None", tmp_path),
        bindings=None,
        ctx=make_context(tmp_path),
    )
    assert result.ok
    assert "hello" in "".join(result.logs)
    assert "world" in "".join(result.logs)


async def test_top_level_await_and_return(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    program = "import asyncio\nawait asyncio.sleep(0.01)\nreturn {'awaited': True}\n"
    result = await runtime.run(run_spec(program, tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert result.ok
    assert result.value == {"awaited": True}


async def test_stderr_is_captured(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    program = "import sys\nprint('oops', file=sys.stderr)\nreturn 1\n"
    result = await runtime.run(run_spec(program, tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert result.ok
    assert result.value == 1
    assert "oops" in "".join(result.logs)


async def test_empty_program_succeeds_with_none(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    result = await runtime.run(run_spec("", tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert result.ok
    assert result.value is None


async def test_binding_success(tmp_path: Path) -> None:
    runtime = PtcRuntime()

    async def read(args: dict[str, object]) -> str:
        assert args == {"file_path": "a.txt"}
        return "file-content"

    program = "text = await tools.read({'file_path': 'a.txt'})\nreturn text\n"
    result = await runtime.run(
        run_spec(program, tmp_path), bindings={"read": read}, ctx=make_context(tmp_path)
    )
    assert result.ok
    assert result.value == "file-content"


async def test_binding_exception_is_catchable(tmp_path: Path) -> None:
    runtime = PtcRuntime()

    async def explode(args: dict[str, object]) -> str:
        raise PtcBindingError("explode", "TOOL_ERROR", "kaput")

    program = "try:\n    await tools.explode()\nexcept Exception as exc:\n    return {'caught': str(exc)}\n"
    result = await runtime.run(
        run_spec(program, tmp_path), bindings={"explode": explode}, ctx=make_context(tmp_path)
    )
    assert result.ok
    assert "kaput" in result.value["caught"]
    assert "TOOL_ERROR" in result.value["caught"]


async def test_undefined_binding_name_is_failure(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    program = "await nope()\n"
    result = await runtime.run(run_spec(program, tmp_path), bindings={}, ctx=make_context(tmp_path))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "exception"
    assert "NameError" in result.failure.message


async def test_unknown_binding_dispatch_is_structured(tmp_path: Path) -> None:
    worker = tmp_path / "call_worker.py"
    worker.write_text(
        "import json, sys\n"
        "sys.stdin.readline()\n"
        "sys.stdout.write(json.dumps({'kind': 'call', 'id': 1, 'name': 'nope', 'arguments': {}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "frame = json.loads(sys.stdin.readline())\n"
        "sys.stdout.write(json.dumps({'kind': 'done', 'value': {'ok': frame.get('ok'), 'error': frame.get('error')}}) + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )
    runtime = PtcRuntime(worker_path=worker)
    result = await runtime.run(run_spec("return 1", tmp_path), bindings={}, ctx=make_context(tmp_path))
    assert result.ok
    assert result.value["ok"] is False
    assert result.value["error"]["code"] == "UNKNOWN_TOOL"
    assert "unknown tool" in result.value["error"]["reason"]


async def test_program_exception_is_failure(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    result = await runtime.run(run_spec("return 1 / 0", tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "exception"
    assert "ZeroDivisionError" in result.failure.message


async def test_syntax_error_is_failure(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    result = await runtime.run(run_spec("return (", tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "exception"
    assert "SyntaxError" in result.failure.message


async def test_timeout_kills_worker(tmp_path: Path) -> None:
    runtime = PtcRuntime(PtcConfig(grace_ms=50))
    started = time.monotonic()
    program = "import asyncio\nawait asyncio.sleep(30)\nreturn 'never'\n"
    result = await asyncio.wait_for(
        runtime.run(run_spec(program, tmp_path, timeout_ms=600), bindings=None, ctx=make_context(tmp_path)),
        timeout=15,
    )
    elapsed = time.monotonic() - started
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "timeout"
    assert "600ms" in result.failure.message
    assert elapsed < 10


async def test_abort_before_start(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    ctx = make_context(tmp_path)
    ctx.cancel.set()
    result = await runtime.run(run_spec("return 1", tmp_path), bindings=None, ctx=ctx)
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "abort"


async def test_abort_while_running(tmp_path: Path) -> None:
    runtime = PtcRuntime(PtcConfig(grace_ms=50))
    ctx = make_context(tmp_path)
    program = "import asyncio\nawait asyncio.sleep(30)\nreturn 'never'\n"
    task = asyncio.create_task(runtime.run(run_spec(program, tmp_path), bindings=None, ctx=ctx))
    await asyncio.sleep(0.5)
    ctx.cancel.set()
    result = await asyncio.wait_for(task, timeout=15)
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "abort"


async def test_worker_exit_before_done(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    result = await runtime.run(
        run_spec("import sys\nsys.exit(7)\n", tmp_path), bindings=None, ctx=make_context(tmp_path)
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "worker-exit"
    assert "7" in result.failure.message


async def test_bad_protocol_frame(tmp_path: Path) -> None:
    worker = tmp_path / "bad_worker.py"
    worker.write_text(
        "import sys\nsys.stdin.readline()\nsys.stdout.write('not json\\n')\nsys.stdout.flush()\n",
        encoding="utf-8",
    )
    runtime = PtcRuntime(worker_path=worker)
    result = await runtime.run(run_spec("return 1", tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "protocol"


async def test_invalid_output(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    result = await runtime.run(
        run_spec("return float('nan')", tmp_path), bindings=None, ctx=make_context(tmp_path)
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "invalid-output"


async def test_output_limit(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    result = await runtime.run(
        run_spec("print('x' * 500)\nreturn None", tmp_path, max_output_bytes=50),
        bindings=None,
        ctx=make_context(tmp_path),
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "output-limit"


async def test_cwd_is_honored(tmp_path: Path) -> None:
    runtime = PtcRuntime()
    program = "import os\nreturn os.getcwd()\n"
    result = await runtime.run(run_spec(program, tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert result.ok
    assert Path(result.value).resolve() == tmp_path.resolve()


async def test_timeout_is_capped_by_config(tmp_path: Path) -> None:
    runtime = PtcRuntime(PtcConfig(max_timeout_ms=300, grace_ms=20))
    program = "import asyncio\nawait asyncio.sleep(30)\nreturn 1\n"
    result = await runtime.run(
        run_spec(program, tmp_path, timeout_ms=60_000), bindings=None, ctx=make_context(tmp_path)
    )
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "timeout"
    assert "300ms" in result.failure.message


async def test_max_pending_calls_limits_concurrency(tmp_path: Path) -> None:
    runtime = PtcRuntime(PtcConfig(max_pending_calls=2))
    active = 0
    peak = 0

    async def slow(args: dict[str, object]) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.15)
        finally:
            active -= 1
        return str(args.get("i"))

    program = "import asyncio\nresults = await asyncio.gather(*[tools.slow({'i': i}) for i in range(6)])\nreturn results\n"
    result = await runtime.run(
        run_spec(program, tmp_path, timeout_ms=20_000), bindings={"slow": slow}, ctx=make_context(tmp_path)
    )
    assert result.ok
    assert result.value == ["0", "1", "2", "3", "4", "5"]
    assert peak == 2


async def test_missing_worker_is_worker_exit(tmp_path: Path) -> None:
    runtime = PtcRuntime(worker_path=tmp_path / "missing_worker.py")
    result = await runtime.run(run_spec("return 1", tmp_path), bindings=None, ctx=make_context(tmp_path))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == "worker-exit"


def test_resolve_caps_timeout_and_accepts_aliases(tmp_path: Path) -> None:
    runtime = PtcRuntime(PtcConfig(max_timeout_ms=1_000))
    spec = runtime.resolve(
        {
            "program": "return 1",
            "timeout_ms": 5_000,
            "max_output_bytes": 123,
            "sandbox_mode": "danger-full-access",
        },
        cwd=tmp_path,
    )
    assert spec.timeout_ms == 1_000
    assert spec.max_output_bytes == 123
    assert spec.sandbox_mode == "danger-full-access"
    assert spec.cwd == tmp_path


@pytest.mark.parametrize(
    "request_value",
    [
        {},
        {"program": 1},
        {"program": "return 1", "timeoutMs": 0},
        {"program": "return 1", "maxOutputBytes": 0},
    ],
)
def test_resolve_rejects_invalid_requests(tmp_path: Path, request_value: dict[str, object]) -> None:
    runtime = PtcRuntime()
    with pytest.raises(ConfigError):
        runtime.resolve(request_value, cwd=tmp_path)


def test_config_defaults_and_camel_case() -> None:
    default = PtcConfig()
    assert default.enabled is False
    assert default.timeout == {"defaultMs": 120_000, "maxMs": 600_000}
    parsed = PtcConfig.from_mapping(
        {
            "enabled": True,
            "executionMode": "per-call",
            "timeoutMs": 2_000,
            "maxTimeoutMs": 3_000,
            "maxOutputBytes": 99,
            "maxPendingCalls": 3,
            "graceMs": 7,
        }
    )
    assert parsed.enabled is True
    assert parsed.timeout_ms == 2_000
    assert parsed.max_timeout_ms == 3_000
    assert parsed.max_output_bytes == 99
    assert parsed.max_pending_calls == 3
    assert parsed.grace_ms == 7


@pytest.mark.parametrize(
    ("mapping", "needle"),
    [
        ({"unknown": 1}, "unknown configuration key"),
        ({"enabled": "yes"}, "enabled"),
        ({"executionMode": "persistent"}, "executionMode"),
        ({"timeoutMs": 0}, "timeoutMs"),
        ({"maxOutputBytes": -1}, "maxOutputBytes"),
        ({"maxPendingCalls": 0}, "maxPendingCalls"),
        ({"graceMs": -1}, "graceMs"),
    ],
)
def test_config_validation(mapping: dict[str, object], needle: str) -> None:
    with pytest.raises(ConfigError) as info:
        PtcConfig.from_mapping(mapping)
    assert needle in str(info.value)


def test_result_to_dict_shape() -> None:
    result = PtcRunResult(
        value={"a": 1},
        logs=("log-line",),
        failure=PtcFailure("exception", "boom"),
        sandbox={"mode": "workspace-write", "enforcement": "none", "denial": "none"},
    )
    payload = result.to_dict()
    assert payload["value"] == {"a": 1}
    assert payload["logs"] == ["log-line"]
    assert payload["failure"] == {"kind": "exception", "message": "boom"}
    assert payload["sandbox"]["enforcement"] == "none"


def test_registry_accepts_reserved_run_code() -> None:
    from deeppyro.ptc import RunCodeTool

    registry = ToolRegistry([RunCodeTool()])
    assert registry.names() == ["run_code"]


def test_config_mode_and_quota_settings() -> None:
    default = PtcConfig()
    assert default.mode == "native"
    assert default.max_parallel_sub_calls == 10
    assert default.max_sub_calls_per_minute == 1_024

    parsed = PtcConfig.from_mapping(
        {
            "mode": "ptc",
            "maxParallelSubCalls": 3,
            "max_sub_calls_per_minute": 50,
        }
    )
    assert parsed.mode == "ptc"
    assert parsed.enabled is True
    assert parsed.max_parallel_sub_calls == 3
    assert parsed.max_sub_calls_per_minute == 50
    assert PtcConfig.from_mapping({"mode": "native", "enabled": True}).enabled is False
    assert PtcConfig.from_mapping({"enabled": False}).mode == "native"
    assert PtcConfig.from_mapping({"enabled": True}).mode == "ptc"


def test_config_rejects_invalid_mode_and_quota() -> None:
    with pytest.raises(ConfigError) as info:
        PtcConfig.from_mapping({"mode": "shell"})
    assert "mode" in str(info.value)
    for mapping, needle in (
        ({"mode": "ptc", "maxParallelSubCalls": 0}, "maxParallelSubCalls"),
        ({"mode": "ptc", "maxParallelSubCalls": True}, "maxParallelSubCalls"),
        ({"mode": "ptc", "maxSubCallsPerMinute": 0}, "maxSubCallsPerMinute"),
        ({"mode": "ptc", "maxSubCallsPerMinute": "fast"}, "maxSubCallsPerMinute"),
    ):
        with pytest.raises(ConfigError) as info:
            PtcConfig.from_mapping(mapping)
        assert needle in str(info.value)


def test_config_to_dict_includes_mode_and_limits() -> None:
    payload = PtcConfig.from_mapping({"mode": "ptc", "maxParallelSubCalls": 4}).to_dict()
    assert payload["mode"] == "ptc"
    assert payload["maxParallelSubCalls"] == 4
    assert payload["maxSubCallsPerMinute"] == 1_024


def test_runtime_presentation_properties() -> None:
    runtime = PtcRuntime(PtcConfig.from_mapping({"mode": "ptc"}))
    assert runtime.mode == "ptc"
    assert runtime.language == "python"
    assert runtime.program_format == "python"
    assert runtime.sandbox_mode is None
    instructions = runtime.execution_instructions
    assert "-I -S" in instructions
    assert "tools.name" in instructions
