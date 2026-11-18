"""Native /hooks: live HookService registry plus recent decisions.

Native sessions have no hook journal in the log (schema 1). /hooks therefore
observes the same HookService the NativeAgentHost dispatches through; these
tests pin the pairing, the registry rendering, the explicit "not recorded"
fallbacks and the panel command wiring against a scripted provider.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.llm import Finish, StopReason, TextDelta, ToolCallArgsDelta, ToolCallStart
from deeppyro.hooks import HOOK_NAMES
from deeppyro.hooks.service import HookService
from deeppyro.tui.app_panels import PanelLayer
from deeppyro.tui.native_hook_trace import (
    NOT_RECORDED,
    UNAVAILABLE_TEXT,
    NativeHookRecorder,
)
from deeppyro.tui.runtime import TuiRuntime
from tests.tui.test_native_runtime import (
    _boot,
    _noop_title,
    _ScriptedProvider,
    _TurnHost,
)


class _FakeHookService:
    """Hook service double: records the observer and reports listener counts."""

    def __init__(self) -> None:
        self.observer: Any = None
        self._listeners: dict[str, tuple[Any, ...]] = {}
        self.removed = 0

    def listeners(self, hook: str) -> tuple[Any, ...]:
        return self._listeners.get(hook, ())

    def add_observer(self, observer: Any) -> Any:
        self.observer = observer

        def remove() -> None:
            self.removed += 1
            self.observer = None

        return remove

    def dispatch(self, hook: str, *, mode: str = "", kind: str = "invoked") -> None:
        assert self.observer is not None
        self.observer(
            SimpleNamespace(
                kind=kind,
                hook=hook,
                mode=mode,
                scope=None,
                payload=None,
                result=None,
                error=None,
                duration_ms=0.0,
            )
        )


def _resolved(service: _FakeHookService, hook: str, *, error: Any = None, duration: float = 3.5) -> None:
    assert service.observer is not None
    service.observer(
        SimpleNamespace(
            kind="resolved",
            hook=hook,
            mode="",
            scope=None,
            payload=None,
            result=None,
            error=error,
            duration_ms=duration,
        )
    )


def test_native_hook_recorder_pairs_decisions_and_counts_errors() -> None:
    service = _FakeHookService()
    recorder = NativeHookRecorder(service, limit=2)
    service.dispatch("tools/pre-execute", mode="waterfall")
    _resolved(service, "tools/pre-execute", duration=4.25)
    service.dispatch("tools/post-execute", mode="waterfall")
    _resolved(service, "tools/post-execute", error=RuntimeError("denied"))

    rows = recorder.rows()
    assert [row["hook"] for row in rows] == ["tools/pre-execute", "tools/post-execute"]
    assert rows[0]["mode"] == "waterfall"
    assert rows[0]["result"] == {"ok": True, "recorded": True, "durationMs": 4.25}
    assert rows[1]["result"]["ok"] is False
    assert "denied" in rows[1]["result"]["error"]
    assert recorder.rows(limit=1) == [rows[1]]

    # The ring keeps the newest decisions only.
    for index in range(3):
        service.dispatch("agent/status")
        _resolved(service, "agent/status", duration=index)
    assert len(recorder.rows()) == 2
    assert recorder.rows()[-1]["hook"] == "agent/status"
    recorder.close()
    assert service.removed == 1


def test_native_hook_recorder_reports_the_registry_and_not_recorded_fallbacks() -> None:
    service = _FakeHookService()
    recorder = NativeHookRecorder(service)
    service._listeners["tools/pre-execute"] = (object(), object())
    rows = recorder.registry_rows()
    assert [row["hook"] for row in rows] == list(HOOK_NAMES)
    pre = next(row for row in rows if row["hook"] == "tools/pre-execute")
    assert pre["mode"] == "waterfall"
    assert pre["listeners"] == 2
    status = next(row for row in rows if row["hook"] == "agent/status")
    assert status["listeners"] == 0

    lines = recorder.registry_lines()
    assert lines[0].startswith("native hook registry:")
    assert any("tools/pre-execute" in line and "listeners=2" in line for line in lines)
    assert lines[-1] == f"  recent hook decisions: {NOT_RECORDED} yet"

    # Without a listener API the registry says "not recorded" instead of faking 0.
    class _Bare:
        def add_observer(self, observer: Any) -> Any:
            return lambda: None

    bare = NativeHookRecorder(_Bare())
    pre = next(row for row in bare.registry_rows() if row["hook"] == "tools/pre-execute")
    assert pre["listeners"] is None
    assert any(f"listeners={NOT_RECORDED}" in line for line in bare.registry_lines())
    assert UNAVAILABLE_TEXT.endswith("(the hook service is unavailable in this bundle)")


class _ToolCallProvider(_ScriptedProvider):
    """One tool call, then a plain text answer, replayed per model call."""

    async def stream(self, request: Any):
        self.calls += 1
        if self.calls == 1:
            events = [
                ToolCallStart(call_id="call-1", name="glob"),
                ToolCallArgsDelta(call_id="call-1", delta='{"pattern": "*.py"}'),
                Finish(StopReason.TOOL_CALLS),
            ]
        else:
            events = [TextDelta("done"), Finish(StopReason.STOP)]
        for event in events:
            yield event


async def test_native_hooks_are_recorded_from_the_live_hook_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorder observes the same HookService the native host dispatches.

    The native loop dispatches the tool pipeline hooks (and session lifecycle
    hooks through the plugin services); it does not run the v4 AgentSession, so
    turn-orchestration hooks stay silent rather than being faked.
    """

    provider = _ToolCallProvider()
    runtime, _events = await _boot(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(TuiRuntime, "name_session_title", _noop_title)
    try:
        recorder = runtime.hook_recorder
        assert isinstance(recorder, NativeHookRecorder)
        assert runtime.kernel_app.get(HookService) is recorder._service
        host = _TurnHost(runtime)
        await host._run_turn("hello")
        hooks = {row["hook"] for row in recorder.rows()}
        assert {"tools/pre-execute", "tools/execute", "tools/post-execute"} <= hooks
        assert all(row["result"] is not None for row in recorder.rows())
        summary = recorder.registry_summary()
        assert "observed" in summary
        assert "recent decision(s)" in summary
    finally:
        await runtime.close()


class _PanelHost(PanelLayer):
    """Minimal host for panel-layer commands without a Textual app."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.refreshes = 0

    @property
    def conversation(self) -> Any:
        return SimpleNamespace(add_info=self.infos.append, add_error=self.errors.append)

    async def push_screen_wait(self, _screen: Any) -> None:
        raise RuntimeError("no modal outside a worker")

    def notify(self, message: str, *, severity: str = "information", **kwargs: Any) -> None:
        self.errors.append(str(message))

    def refresh_status(self) -> None:
        self.refreshes += 1

    def _native_hook_recorder(self) -> Any:
        return PanelLayer._native_hook_recorder(self)


async def test_native_hooks_command_renders_registry_and_recent_decisions() -> None:
    service = _FakeHookService()
    recorder = NativeHookRecorder(service)
    service.dispatch("tools/pre-execute", mode="waterfall")
    _resolved(service, "tools/pre-execute", duration=2.0)
    host = _PanelHost(SimpleNamespace(hook_recorder=recorder))

    await host.hooks_command()

    assert host.infos[0].startswith("native hook registry:")
    assert any("tools/pre-execute" in line and "mode=waterfall" in line for line in host.infos)
    assert any("tools/pre-execute" in line and "ok" in line for line in host.infos)
    assert host.errors == []


async def test_native_hooks_command_says_not_recorded_without_a_service() -> None:
    host = _PanelHost(SimpleNamespace(hook_recorder=None))
    await host.hooks_command()
    assert host.infos == [UNAVAILABLE_TEXT]

    recorder = NativeHookRecorder(_FakeHookService())
    host = _PanelHost(SimpleNamespace(hook_recorder=recorder))
    await host.hooks_command()
    assert host.infos[0].startswith("native hook registry:")
    assert host.infos[-1] == "no hook activity yet"
