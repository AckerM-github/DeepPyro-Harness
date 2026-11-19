"""Unit tests for the standalone PTC worker entry point."""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import pytest

from deeppyro.ptc import worker


class ImmediateExit(BaseException):
    """Sentinel that replaces os._exit so the test process survives."""

    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class FakeStdin:
    def __init__(self, raw: bytes) -> None:
        self.buffer = io.BytesIO(raw)


def test_system_exit_leaves_through_the_os(monkeypatch: pytest.MonkeyPatch) -> None:
    """A program's sys.exit must not reach CPython's shutdown path.

    The worker keeps a daemon reader blocked on stdin.  Returning through the
    normal interpreter teardown makes Windows CPython 3.13 crash in
    python313.dll after a program calls sys.exit; the worker now flushes and
    calls os._exit instead, preserving the exit code for the host.
    """
    request = json.dumps({"protocol": 1, "program": "pass", "bindings": []}).encode() + b"\n"
    saved = (sys.stdin, sys.stdout, sys.stderr)
    monkeypatch.setattr(worker.sys, "stdin", FakeStdin(request))
    monkeypatch.setattr(worker.sys, "stdout", io.StringIO())
    monkeypatch.setattr(worker.sys, "stderr", io.StringIO())

    def fake_run(awaitable: Any) -> None:
        awaitable.close()
        raise SystemExit(7)

    def fake_exit(code: int) -> None:
        raise ImmediateExit(code)

    monkeypatch.setattr(worker.asyncio, "run", fake_run)
    monkeypatch.setattr(worker.os, "_exit", fake_exit)
    try:
        with pytest.raises(ImmediateExit) as raised:
            worker.main()
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved
    assert raised.value.code == 7
