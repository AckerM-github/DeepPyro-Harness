"""Standalone worker executed once per run_code call.

This module is started as a subprocess by deeppyro.ptc.runtime.  It only uses
the Python standard library so it works under "python -I -S" and never imports
the harness itself.

Wire protocol (one JSON object per line, UTF-8):

  stdin  <- {"protocol": 1, "program": "...", "bindings": ["read", ...]}
  stdout -> {"kind": "log", "stream": "stdout", "text": "..."}
  stdout -> {"kind": "call", "id": 1, "name": "read", "arguments": {...}}
  stdin  <- {"kind": "result", "id": 1, "ok": true, "value": "..."}
  stdin  <- {"kind": "result", "id": 1, "ok": false, "error": {"name": ..., "code": ..., "reason": ...}}
  stdout -> {"kind": "done", "value": ...}
  stdout -> {"kind": "error", "errorKind": "exception", "message": "..."}

The program is compiled as the body of an async function, so it may use
top-level await and return.  Tool bindings are reachable only through the
"tools" object: tools.name(args) and tools["name"](args) are async and return
the tool result.  Program failures surface as PtcToolCallError.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sys
import threading
from typing import Any

PROTOCOL_VERSION = 1
MAX_LOG_BYTES_PER_STREAM = 1024 * 1024


class PtcToolCallError(Exception):
    """Raised inside the program when a host tool binding fails."""

    def __init__(self, name: str, code: str, reason: str = "") -> None:
        super().__init__(reason or code)
        self.name = name
        self.toolName = name
        self.code = code
        self.reason = reason

    def __str__(self) -> str:
        if self.reason:
            return f"{self.name}: {self.reason} ({self.code})"
        return f"{self.name}: {self.code}"


class OutputLimitError(Exception):
    """Raised when the program returns a value that is not lossless JSON."""


class ProtocolError(Exception):
    pass


class _Protocol:
    def __init__(self) -> None:
        self._stream = sys.__stdout__
        self._lock = threading.Lock()

    def send(self, frame: dict[str, Any]) -> None:
        line = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def send_or_ignore(self, frame: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self.send(frame)


class _LogWriter:
    """Captures print() output and forwards it as log frames."""

    def __init__(self, protocol: _Protocol, stream: str) -> None:
        self._protocol = protocol
        self._stream = stream
        self._sent = 0
        self._truncated = False
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        encoded = text.encode("utf-8", "replace")
        with self._lock:
            if self._truncated:
                return len(text)
            remaining = MAX_LOG_BYTES_PER_STREAM - self._sent
            if remaining <= 0:
                self._mark_truncated()
                return len(text)
            if len(encoded) > remaining:
                clipped = encoded[:remaining].decode("utf-8", "ignore")
                self._sent += len(clipped.encode("utf-8", "replace"))
                self._protocol.send_or_ignore({"kind": "log", "stream": self._stream, "text": clipped})
                self._mark_truncated()
                return len(text)
            self._sent += len(encoded)
        self._protocol.send_or_ignore({"kind": "log", "stream": self._stream, "text": text})
        return len(text)

    def _mark_truncated(self) -> None:
        self._truncated = True
        self._protocol.send_or_ignore(
            {"kind": "log", "stream": self._stream, "text": "[... output truncated by run_code ...]\n"}
        )

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"

    @property
    def errors(self) -> str:
        return "replace"


class _Reader(threading.Thread):
    def __init__(self, loop: asyncio.AbstractEventLoop, on_frame: Any, on_eof: Any) -> None:
        super().__init__(name="ptc-worker-reader", daemon=True)
        self._loop = loop
        self._on_frame = on_frame
        self._on_eof = on_eof

    def run(self) -> None:
        stream = sys.stdin.buffer
        while True:
            try:
                raw = stream.readline()
            except Exception as exc:  # noqa: BLE001 - stdin errors are fatal and reported as EOF
                # pragma: no cover - stdin errors are rare
                self._loop.call_soon_threadsafe(self._on_eof, f"cannot read protocol input: {exc}")
                return
            if not raw:
                self._loop.call_soon_threadsafe(self._on_eof, "host closed the protocol input")
                return
            try:
                frame = json.loads(raw.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - malformed input is a protocol error
                self._loop.call_soon_threadsafe(self._on_eof, f"invalid protocol frame: {exc}")
                return
            if not isinstance(frame, dict):
                self._loop.call_soon_threadsafe(self._on_eof, "protocol frame must be an object")
                return
            self._loop.call_soon_threadsafe(self._on_frame, frame)


def _normalize_json(value: Any, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OutputLimitError("the return value contains a non-finite float")
        return value
    if isinstance(value, dict):
        seen = _seen if _seen is not None else set()
        if id(value) in seen:
            raise OutputLimitError("the return value contains a cycle")
        seen.add(id(value))
        try:
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise OutputLimitError("JSON object keys must be strings")
                normalized[key] = _normalize_json(item, seen)
            return normalized
        finally:
            seen.discard(id(value))
    if isinstance(value, (list, tuple)):
        seen = _seen if _seen is not None else set()
        if id(value) in seen:
            raise OutputLimitError("the return value contains a cycle")
        seen.add(id(value))
        try:
            return [_normalize_json(item, seen) for item in value]
        finally:
            seen.discard(id(value))
    raise OutputLimitError(f"the return value is not JSON serializable: {type(value).__name__}")


class _ToolsProxy:
    """The only tool surface bound into a program's namespace.

    Attribute access and item access both produce an async binding, so programs
    can write either tools.read({...}) or tools["read"]({...}).
    """

    def __init__(self, program: _Program) -> None:
        self._program = program

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self._program._make_binding(name)

    def __getitem__(self, name: str) -> Any:
        if not isinstance(name, str):
            raise TypeError("tool name must be a string")
        return self._program._make_binding(name)


class _Program:
    def __init__(self, protocol: _Protocol, program: str, binding_names: list[str]) -> None:
        self._protocol = protocol
        self._program = program
        self._binding_names = binding_names
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id = 0
        self._closed = False

    def _make_binding(self, name: str) -> Any:
        async def binding(arguments: Any = None, **kwargs: Any) -> str:
            if arguments is None:
                payload = dict(kwargs)
            elif isinstance(arguments, dict):
                payload = dict(arguments)
                payload.update(kwargs)
            else:
                raise TypeError(f"{name}() expects a JSON object of arguments")
            return await self._call(name, payload)

        binding.__name__ = name
        return binding

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        if self._closed:
            raise PtcToolCallError(name, "PROTOCOL", "the worker is shutting down")
        self._next_id += 1
        call_id = self._next_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[call_id] = future
        try:
            self._protocol.send({"kind": "call", "id": call_id, "name": name, "arguments": arguments})
            return await future
        finally:
            self._pending.pop(call_id, None)

    def on_frame(self, frame: dict[str, Any]) -> None:
        if frame.get("kind") != "result":
            self._fail_all(ProtocolError(f"unexpected protocol frame: {frame.get('kind')!r}"))
            return
        call_id = frame.get("id")
        future = self._pending.get(call_id) if isinstance(call_id, int) else None
        if future is None or future.done():
            return
        if frame.get("ok") is True:
            value = frame.get("value")
            future.set_result(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
            return
        error = frame.get("error")
        if not isinstance(error, dict):
            error = {}
        name = str(error.get("name") or "tool")
        code = str(error.get("code") or "TOOL_ERROR")
        reason = str(error.get("reason") or "tool call failed")
        future.set_exception(PtcToolCallError(name, code, reason))

    def on_eof(self, reason: str) -> None:
        self._fail_all(ProtocolError(reason))

    def _fail_all(self, exc: Exception) -> None:
        self._closed = True
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)

    async def run(self) -> Any:
        loop = asyncio.get_running_loop()
        reader = _Reader(loop, self.on_frame, self.on_eof)
        reader.start()
        namespace: dict[str, Any] = {
            "__name__": "__ptc_program__",
            "tools": _ToolsProxy(self),
            "ToolCallError": PtcToolCallError,
        }
        body = self._program if self._program.strip() else "pass"
        source = "async def __ptc_main__():\n" + textwrap_indent(body, "    ")
        code = compile(source, "<run_code>", "exec")
        exec(code, namespace)  # noqa: S102 - deliberate: this is the PTC execution model
        main = namespace["__ptc_main__"]
        return await main()


def textwrap_indent(text: str, prefix: str) -> str:
    lines = text.splitlines()
    if not lines:
        return prefix + "pass"
    return "\n".join(prefix + line if line.strip() else "" for line in lines) + "\n"


def _describe_exception(exc: BaseException) -> str:
    message = str(exc)
    label = type(exc).__name__
    return f"{label}: {message}" if message else label


async def _main(protocol: _Protocol, program: str, binding_names: list[str]) -> None:
    runner = _Program(protocol, program, binding_names)
    try:
        value = await runner.run()
    except asyncio.CancelledError:
        raise
    except SyntaxError as exc:
        protocol.send(
            {
                "kind": "error",
                "errorKind": "exception",
                "message": f"SyntaxError: {exc.msg} (line {exc.lineno})",
            }
        )
    except Exception as exc:  # noqa: BLE001 - all program exceptions become structured failures
        protocol.send({"kind": "error", "errorKind": "exception", "message": _describe_exception(exc)})
    else:
        try:
            normalized = _normalize_json(value)
            encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False)
        except (OutputLimitError, TypeError, ValueError) as exc:
            protocol.send({"kind": "error", "errorKind": "invalid-output", "message": str(exc)})
            return
        protocol.send({"kind": "done", "value": normalized, "encodedBytes": len(encoded.encode("utf-8"))})


def main() -> int:
    try:
        first = sys.stdin.buffer.readline()
    except Exception:  # noqa: BLE001 - startup failure is reported by the exit code
        return 2
    if not first:
        return 2
    try:
        request = json.loads(first.decode("utf-8"))
    except Exception:  # noqa: BLE001 - startup failure is reported by the exit code
        return 2
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL_VERSION:
        return 2
    program = request.get("program")
    bindings = request.get("bindings")
    if (
        not isinstance(program, str)
        or not isinstance(bindings, list)
        or not all(isinstance(name, str) for name in bindings)
    ):
        return 2
    protocol = _Protocol()
    sys.stdout = _LogWriter(protocol, "stdout")  # type: ignore[assignment]
    sys.stderr = _LogWriter(protocol, "stderr")  # type: ignore[assignment]
    try:
        asyncio.run(_main(protocol, program, bindings))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
