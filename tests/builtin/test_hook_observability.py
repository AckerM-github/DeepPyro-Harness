"""Tests for the builtin hook-observability plugin (task-63)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from deeppyro.builtin.hook_observability import (
    OBSERVED_HOOKS,
    WATERFALL_HOOKS,
    HookObservabilityPlugin,
)
from deeppyro.core.protocols import TextDelta
from deeppyro.hooks import (
    AGENT_ERROR,
    APPROVAL_ASKED,
    APPROVAL_DECIDED,
    APPROVAL_REQUEST,
    LLM_STREAM,
    MODEL_SELECTION,
    SESSION_CREATED,
    SESSION_DISPOSED,
    SYSTEM_PROMPT_ASSEMBLE,
    TOOL_CALL,
    HookService,
)
from deeppyro.sandbox import ApprovalRequest, PermissionGate

LOGGER = "deeppyro.hooks"


class FakeCtx:
    """Minimal PluginContext: service lookup and effect bookkeeping."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.effects: list[str] = []
        self.disposers: list[Any] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        dispose = execute()
        self.effects.append(label)
        self.disposers.append(dispose)
        return dispose


def make_plugin() -> tuple[HookObservabilityPlugin, HookService, FakeCtx]:
    hooks = HookService()
    ctx = FakeCtx({"hooks": hooks})
    plugin = HookObservabilityPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, hooks, ctx


def messages(caplog: pytest.LogCaptureFixture, hook: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER and record.getMessage().startswith(f"hook {hook} ")
    ]


EMIT_HOOKS = (
    LLM_STREAM,
    TOOL_CALL,
    SESSION_CREATED,
    SESSION_DISPOSED,
    APPROVAL_ASKED,
    APPROVAL_DECIDED,
    AGENT_ERROR,
)


# -- registration ----------------------------------------------------------
def test_observed_hooks_cover_the_ten_new_hooks() -> None:
    assert len(OBSERVED_HOOKS) == 10
    assert len(set(OBSERVED_HOOKS)) == 10
    assert set(OBSERVED_HOOKS) == {
        SYSTEM_PROMPT_ASSEMBLE,
        LLM_STREAM,
        MODEL_SELECTION,
        TOOL_CALL,
        SESSION_CREATED,
        SESSION_DISPOSED,
        APPROVAL_REQUEST,
        APPROVAL_ASKED,
        APPROVAL_DECIDED,
        AGENT_ERROR,
    }
    assert WATERFALL_HOOKS == {
        SYSTEM_PROMPT_ASSEMBLE,
        MODEL_SELECTION,
        APPROVAL_REQUEST,
    }


def test_plugin_metadata() -> None:
    assert HookObservabilityPlugin.plugin_id == "hook-observability"
    assert HookObservabilityPlugin.inject == ["hooks"]


def test_setup_registers_a_listener_for_every_hook() -> None:
    plugin, hooks, ctx = make_plugin()

    for name in OBSERVED_HOOKS:
        assert hooks.has(name) is True
    assert ctx.effects == [f"observe:{name}" for name in OBSERVED_HOOKS]
    assert plugin.snapshot() == dict.fromkeys(OBSERVED_HOOKS, 0)


def test_setup_requires_the_hooks_service() -> None:
    ctx = FakeCtx({})
    with pytest.raises(LookupError):
        HookObservabilityPlugin(ctx, None).setup(ctx, None)


# -- emit hooks ------------------------------------------------------------
@pytest.mark.parametrize("name", EMIT_HOOKS)
async def test_every_emit_hook_is_counted_and_logged(name: str, caplog: pytest.LogCaptureFixture) -> None:
    plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.DEBUG)

    await hooks.emit(name, {"sessionId": "session-1"})

    assert plugin.snapshot()[name] == 1
    assert messages(caplog, name) == ["hook " + name + " sessionId='session-1'"]


async def test_llm_stream_and_tool_call_log_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.DEBUG)

    await hooks.emit(LLM_STREAM, {"provider": "local", "model": "m"})
    await hooks.emit(TOOL_CALL, {"tool": "read"})

    records = [record for record in caplog.records if record.name == LOGGER]
    assert [record.levelno for record in records] == [logging.DEBUG, logging.DEBUG]
    assert records[0].getMessage() == "hook llm/stream provider='local' model='m'"
    assert records[1].getMessage() == "hook tool/call tool='read'"


async def test_session_hooks_log_at_info(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO)

    await hooks.emit(SESSION_CREATED, {"sessionId": "session-1", "cwd": "D:\\work"})

    records = [record for record in caplog.records if record.name == LOGGER]
    assert [record.levelno for record in records] == [logging.INFO]
    assert records[0].getMessage() == ("hook session/created sessionId='session-1' cwd='D:\\\\work'")


# -- waterfall hooks -------------------------------------------------------
@pytest.mark.parametrize("name", sorted(WATERFALL_HOOKS))
async def test_waterfall_observers_delegate_to_the_fallback(name: str) -> None:
    plugin, hooks, _ = make_plugin()

    assert await hooks.waterfall(name, {"turn": 1}, fallback="fallback") == "fallback"
    assert plugin.snapshot()[name] == 1


async def test_waterfall_observer_returns_the_downstream_value() -> None:
    plugin, hooks, _ = make_plugin()

    def downstream(payload: dict[str, Any], next_step: Any) -> str:
        return "deny"

    hooks.on(APPROVAL_REQUEST, downstream)

    assert await hooks.waterfall(APPROVAL_REQUEST, {}, fallback="ask") == "deny"
    assert plugin.snapshot()[APPROVAL_REQUEST] == 1


async def test_waterfall_sections_are_rendered_by_length(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.INFO)

    await hooks.waterfall(
        SYSTEM_PROMPT_ASSEMBLE,
        {"turn": 2, "step": 3, "sections": ("a", "b", "c")},
        fallback=None,
    )

    assert messages(caplog, SYSTEM_PROMPT_ASSEMBLE) == [
        "hook system-prompt/assemble turn=2 step=3 sections=3"
    ]


# -- payload rendering -----------------------------------------------------
async def test_frames_render_their_type_name(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.DEBUG)

    await hooks.emit(LLM_STREAM, {"frame": TextDelta(text="hi")})

    assert messages(caplog, LLM_STREAM) == ["hook llm/stream frame=TextDelta"]


async def test_requests_render_their_kind(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.INFO)
    request = ApprovalRequest(kind="write", summary="write a.txt")

    await hooks.emit(APPROVAL_DECIDED, {"request": request, "allowed": True})

    assert messages(caplog, APPROVAL_DECIDED) == ["hook approval/decided allowed=True request='write'"]


async def test_policy_and_reason_render(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.INFO)

    await hooks.emit(
        APPROVAL_DECIDED,
        {"allowed": False, "reason": "rejected by the user", "via": "user", "policy": "ask"},
    )

    assert messages(caplog, APPROVAL_DECIDED) == [
        "hook approval/decided allowed=False reason='rejected by the user' via='user' policy='ask'"
    ]


async def test_unknown_payload_keys_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.INFO)

    await hooks.emit(SESSION_DISPOSED, {"sessionId": "session-1", "noise": object()})

    assert messages(caplog, SESSION_DISPOSED) == ["hook session/disposed sessionId='session-1'"]


# -- snapshot and disposal -------------------------------------------------
async def test_snapshot_tracks_repeated_dispatches() -> None:
    plugin, hooks, _ = make_plugin()

    await hooks.emit(TOOL_CALL, {"tool": "read"})
    await hooks.emit(TOOL_CALL, {"tool": "write"})
    await hooks.waterfall(MODEL_SELECTION, {"model": "m"}, fallback=None)

    snapshot = plugin.snapshot()
    assert snapshot[TOOL_CALL] == 2
    assert snapshot[MODEL_SELECTION] == 1
    assert snapshot[SESSION_CREATED] == 0
    snapshot[TOOL_CALL] = 99
    assert plugin.snapshot()[TOOL_CALL] == 2


async def test_close_removes_every_listener() -> None:
    plugin, hooks, _ = make_plugin()

    plugin.close()

    for name in OBSERVED_HOOKS:
        assert hooks.has(name) is False
    plugin.close()
    for name in OBSERVED_HOOKS:
        assert hooks.has(name) is False


async def test_no_logging_after_close(caplog: pytest.LogCaptureFixture) -> None:
    plugin, hooks, _ = make_plugin()
    plugin.close()
    caplog.set_level(logging.DEBUG)

    await hooks.emit(SESSION_CREATED, {"sessionId": "session-1"})

    assert messages(caplog, SESSION_CREATED) == []
    assert plugin.snapshot()[SESSION_CREATED] == 0


# -- real dispatch path: approval gate ------------------------------------
async def test_approval_gate_dispatches_asked_and_decided(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.INFO)
    asked: list[ApprovalRequest] = []

    async def ask(request: ApprovalRequest) -> bool:
        asked.append(request)
        return True

    gate = PermissionGate("workspace-write", "ask", "D:\\work", ask=ask, hooks=hooks)
    request = ApprovalRequest(kind="write", summary="write a.txt")

    decision = await gate.request(request)

    assert decision.allowed is True
    assert asked == [request]
    assert messages(caplog, APPROVAL_ASKED) == ["hook approval/asked policy='ask' request='write'"]
    assert messages(caplog, APPROVAL_DECIDED) == [
        ("hook approval/decided allowed=True reason='approved by the user' via='user' request='write'")
    ]
    assert _plugin.snapshot()[APPROVAL_ASKED] == 1
    assert _plugin.snapshot()[APPROVAL_DECIDED] == 1


async def test_approval_gate_records_denied_decision(caplog: pytest.LogCaptureFixture) -> None:
    _plugin, hooks, _ = make_plugin()
    caplog.set_level(logging.INFO)

    async def ask(request: ApprovalRequest) -> bool:
        return False

    gate = PermissionGate("workspace-write", "ask", "D:\\work", ask=ask, hooks=hooks)

    decision = await gate.request(ApprovalRequest(kind="command", summary="rm -rf /"))

    assert decision.allowed is False
    assert messages(caplog, APPROVAL_DECIDED) == [
        ("hook approval/decided allowed=False reason='rejected by the user' via='user' request='command'")
    ]


async def test_approval_gate_dispatches_decided_without_asked_for_never() -> None:
    plugin, hooks, _ = make_plugin()

    gate = PermissionGate("workspace-write", "never", "D:\\work", hooks=hooks)
    decision = await gate.request(ApprovalRequest(kind="write", summary="write a.txt"))

    assert decision.allowed is False
    assert plugin.snapshot()[APPROVAL_ASKED] == 0
    assert plugin.snapshot()[APPROVAL_DECIDED] == 1
