"""Tests for the builtin llm-retry plugin: policy construction and hook consumer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.agent import AgentOptions, AgentSession
from deeppyro.agent.retry import DEFAULT_INITIAL_DELAY_MS, DEFAULT_JITTER_RATIO, DEFAULT_MAX_DELAY_MS
from deeppyro.builtin.llm_retry import LlmRetryPlugin, build_retry_policy
from deeppyro.config import Config
from deeppyro.core.errors import ConfigError, LlmError
from deeppyro.core.protocols import TextDelta
from deeppyro.hooks import HookService
from deeppyro.hooks.types import AGENT_REQUEST_ERROR
from deeppyro.llm.errors import LlmRequestError
from deeppyro.llm.retry import RetryPolicy
from tests.agent.fakes import (
    FakePermission,
    FakeRegistry,
    FakeSessionLog,
    RecordingSink,
    ScriptedProvider,
    ScriptedTurn,
    finish,
)


# -- helpers ---------------------------------------------------------------
class FakeCtx:
    """Minimal PluginContext: service lookup, provide and effect disposal."""

    def __init__(self, services: dict[str, Any]) -> None:
        self._services = dict(services)
        self.provided: dict[str, Any] = {}
        self.effects: list[str] = []
        self.disposers: list[Any] = []

    def service(self, name: str) -> Any:
        if name not in self._services:
            raise LookupError(f"service {name!r} is not registered")
        return self._services[name]

    def provide(self, name: str, value: Any, check: Any = None) -> Any:
        self.provided[name] = value

        def dispose() -> None:
            self.provided.pop(name, None)

        return dispose

    def effect(self, execute: Any, label: str = "anonymous") -> Any:
        dispose = execute()
        self.effects.append(label)
        self.disposers.append(dispose)
        return dispose


def make_config(retry: dict[str, Any] | None = None) -> Config:
    data: dict[str, Any] = {
        "agent": {"provider": "local", "model": "m"},
        "providers": {
            "local": {
                "api": "openai-completions",
                "baseURL": "http://127.0.0.1:8000/v1",
                "models": [{"id": "m"}],
            }
        },
    }
    if retry is not None:
        data["retry"] = retry
    return Config.model_validate(data)


def make_plugin(
    retry: dict[str, Any] | None = None,
) -> tuple[LlmRetryPlugin, FakeCtx, HookService]:
    hooks = HookService()
    ctx = FakeCtx({"config": SimpleNamespace(value=make_config(retry)), "hooks": hooks})
    plugin = LlmRetryPlugin(ctx, None)
    plugin.setup(ctx, None)
    return plugin, ctx, hooks


def make_agent(
    tmp_path: Path,
    script: list[ScriptedTurn],
    services: dict[str, Any],
) -> tuple[FakeSessionLog, ScriptedProvider, RecordingSink, AgentSession]:
    session = FakeSessionLog(cwd=tmp_path)
    provider = ScriptedProvider(script)
    sink = RecordingSink(session)
    agent = AgentSession(
        session=session,
        provider=provider,
        registry=FakeRegistry(),
        options=AgentOptions(model="test-model"),
        sink=sink,
        permission=FakePermission(),
        cwd=tmp_path,
        services=services,
    )
    return session, provider, sink, agent


async def next_returns(value: Any) -> Any:
    return value


# -- policy construction ---------------------------------------------------
def test_plugin_metadata() -> None:
    plugin = LlmRetryPlugin.__new__(LlmRetryPlugin)
    assert LlmRetryPlugin.provide == "retryPolicy"
    assert LlmRetryPlugin.plugin_id == "llm-retry"
    assert LlmRetryPlugin.version == "0.1.0"
    assert "hooks" in LlmRetryPlugin.inject and "config" in LlmRetryPlugin.inject
    assert LlmRetryPlugin.description
    assert not hasattr(plugin, "policy") or plugin.policy is None


def test_missing_table_uses_retry_policy_defaults() -> None:
    policy = build_retry_policy(None)
    assert isinstance(policy, RetryPolicy)
    assert policy.max_attempts == 3
    assert policy.base_delay == 2.0
    assert policy.max_delay == 30.0
    assert policy.jitter == 0.1
    assert policy.retryable_codes is None


def test_normal_table_builds_retry_policy() -> None:
    policy = build_retry_policy(
        {
            "mode": "normal",
            "maxRetries": 5,
            "initialDelayMs": 500,
            "maxDelayMs": 10000,
            "jitterRatio": 0.1,
            "retryableCodes": ["SERVER", "RATE_LIMIT", "TIMEOUT"],
        }
    )
    assert isinstance(policy, RetryPolicy)
    assert policy.max_attempts == 6
    assert policy.base_delay == 0.5
    assert policy.max_delay == 10.0
    assert policy.jitter == 0.1
    assert policy.retryable_codes == frozenset({"SERVER", "RATE_LIMIT", "TIMEOUT"})


def test_missing_normal_fields_fall_back_to_dsh_defaults() -> None:
    policy = build_retry_policy({"mode": "normal"})
    assert isinstance(policy, RetryPolicy)
    assert policy.max_attempts == 6
    assert policy.base_delay == DEFAULT_INITIAL_DELAY_MS / 1000.0
    assert policy.max_delay == DEFAULT_MAX_DELAY_MS / 1000.0
    assert policy.jitter == DEFAULT_JITTER_RATIO
    assert policy.retryable_codes is None


def test_explicit_empty_codes_means_no_retryable_codes() -> None:
    policy = build_retry_policy({"retryableCodes": []})
    assert isinstance(policy, RetryPolicy)
    assert policy.retryable_codes == frozenset()


def test_camel_and_snake_case_keys_are_accepted() -> None:
    policy = build_retry_policy(
        {
            "max_retries": 2,
            "initial_delay_ms": 100,
            "max_delay_ms": 1000,
            "jitter_ratio": 0.0,
            "retryable_codes": ["SERVER"],
        }
    )
    assert isinstance(policy, RetryPolicy)
    assert policy.max_attempts == 3
    assert policy.base_delay == 0.1
    assert policy.max_delay == 1.0
    assert policy.jitter == 0.0
    assert policy.retryable_codes == frozenset({"SERVER"})


def test_always_mode_returns_mapping() -> None:
    policy = build_retry_policy(
        {"mode": "always", "initialDelayMs": 0, "maxDelayMs": 1000, "jitterRatio": 0.0}
    )
    assert policy == {
        "mode": "always",
        "initialDelayMs": 0.0,
        "maxDelayMs": 1000.0,
        "jitterRatio": 0.0,
    }


def test_mode_is_case_insensitive() -> None:
    policy = build_retry_policy({"mode": "ALWAYS", "initialDelayMs": 1, "maxDelayMs": 2})
    assert policy["mode"] == "always"


@pytest.mark.parametrize(
    ("retry", "message"),
    [
        ({"mode": "sometimes"}, "mode must be"),
        ({"maxRetries": -1}, "maxRetries"),
        ({"maxRetries": True}, "maxRetries"),
        ({"maxRetries": 1.5}, "maxRetries"),
        ({"initialDelayMs": -1}, "initialDelayMs"),
        ({"initialDelayMs": "fast"}, "initialDelayMs"),
        ({"maxDelayMs": 0}, "maxDelayMs"),
        ({"initialDelayMs": 2000, "maxDelayMs": 1000}, "initialDelayMs must be"),
        ({"jitterRatio": 1.5}, "jitterRatio"),
        ({"retryableCodes": "SERVER"}, "retryableCodes"),
        ({"retryableCodes": [""]}, "retryableCodes"),
        ({"retryableCodes": [1]}, "retryableCodes"),
    ],
)
def test_invalid_table_raises_config_error(retry: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        build_retry_policy(retry)


def test_non_mapping_table_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="must be a table"):
        build_retry_policy(["normal"])


# -- setup / service registration -----------------------------------------
def test_setup_registers_policy_service_and_listener() -> None:
    plugin, ctx, hooks = make_plugin({"initialDelayMs": 0, "maxDelayMs": 1000})
    assert ctx.provided["retryPolicy"] is plugin.policy
    assert hooks.has(AGENT_REQUEST_ERROR)
    assert plugin._on_request_error in hooks.listeners(AGENT_REQUEST_ERROR)


def test_setup_effect_disposes_the_listener() -> None:
    _plugin, ctx, hooks = make_plugin()
    assert ctx.effects == ["agent-request-error"]
    assert hooks.has(AGENT_REQUEST_ERROR)
    ctx.disposers[0]()
    assert not hooks.has(AGENT_REQUEST_ERROR)


def test_policy_comes_from_the_config_service() -> None:
    table = {"mode": "normal", "maxRetries": 1, "initialDelayMs": 250, "maxDelayMs": 500}
    plugin, _ctx, _hooks = make_plugin(table)
    expected = build_retry_policy(table)
    assert plugin.policy == expected


# -- hook handler ----------------------------------------------------------
async def test_handler_passes_through_without_a_policy() -> None:
    plugin, _ctx, _hooks = make_plugin()
    calls: list[str] = []

    async def next_step() -> Any:
        calls.append("next")
        return {"kind": "continue"}

    result = await plugin._on_request_error({"retryPolicy": None}, next_step)
    assert result == {"kind": "continue"}
    assert calls == ["next"]


async def test_handler_returns_retry_when_policy_present() -> None:
    plugin, _ctx, _hooks = make_plugin()
    calls: list[str] = []

    async def next_step() -> Any:
        calls.append("next")
        return None

    result = await plugin._on_request_error({"retryPolicy": plugin.policy}, next_step)
    assert result == {"kind": "retry"}
    assert calls == []


async def test_handler_declines_when_signal_is_aborted() -> None:
    plugin, _ctx, _hooks = make_plugin()
    signal = asyncio.Event()
    signal.set()
    calls: list[str] = []

    async def next_step() -> Any:
        calls.append("next")
        return None

    result = await plugin._on_request_error({"retryPolicy": plugin.policy, "signal": signal}, next_step)
    assert result is None
    assert calls == ["next"]


async def test_waterfall_short_circuits_with_retry() -> None:
    plugin, _ctx, hooks = make_plugin()
    result = await hooks.waterfall(
        AGENT_REQUEST_ERROR,
        {"retryPolicy": plugin.policy},
        fallback=lambda: {"kind": "none"},
    )
    assert result == {"kind": "retry"}


async def test_waterfall_without_policy_reaches_the_fallback() -> None:
    _plugin, _ctx, hooks = make_plugin()
    result = await hooks.waterfall(
        AGENT_REQUEST_ERROR,
        {"retryPolicy": None},
        fallback=lambda: {"kind": "none"},
    )
    assert result == {"kind": "none"}


# -- integration with AgentSession -----------------------------------------
def _retry_policy_from(retry: dict[str, Any]) -> Any:
    plugin, _ctx, hooks = make_plugin(retry)
    return plugin.policy, hooks


async def test_failed_request_is_retried_and_logged(tmp_path: Path) -> None:
    policy, hooks = _retry_policy_from(
        {
            "mode": "normal",
            "maxRetries": 5,
            "initialDelayMs": 0,
            "maxDelayMs": 1000,
            "jitterRatio": 0.0,
            "retryableCodes": ["RATE_LIMIT", "SERVER"],
        }
    )
    script = [
        ScriptedTurn(
            error=LlmRequestError(
                "429 rate limited", provider="fake-provider", status=429, retryable=True, code="RATE_LIMIT"
            )
        ),
        ScriptedTurn(events=[TextDelta("ok"), finish()]),
    ]
    session, provider, sink, agent = make_agent(tmp_path, script, {"hooks": hooks, "retryPolicy": policy})

    assert await agent.send("go") == "ok"

    assert provider.calls == 2
    types = session.types()
    assert types.count("step/start") == 1
    assert types.count("assistant/attempt") == 1
    assert types.count("assistant/message") == 1
    attempt_at = types.index("assistant/attempt")
    retry_at = types.index("llm/retry")
    started_at = types.index("llm/retry-started")
    message_at = types.index("assistant/message")
    assert attempt_at < retry_at < started_at < message_at

    record = session.records_of("llm/retry")[0].data
    assert record["retry"] == 1
    assert record["mode"] == "normal"
    assert record["maxRetries"] == 5
    assert record["policyKey"] == '["normal",5,["RATE_LIMIT","SERVER"],0,1000,0]'
    assert record["delayMs"] == 0.0
    assert isinstance(record["retryId"], str) and record["retryId"]
    assert record["failure"] == {
        "message": "429 rate limited",
        "code": "RATE_LIMIT",
        "status": 429,
    }
    started = session.records_of("llm/retry-started")[0].data
    assert started == {
        "retryId": record["retryId"],
        "turn": 1,
        "step": 1,
        "retry": 1,
    }
    assert session.records_of("turn/end")[0].data["reason"] == {"kind": "completed"}
    assert sink.of_type("error") == []


async def test_always_mode_retries_unknown_failure_codes(tmp_path: Path) -> None:
    policy, hooks = _retry_policy_from(
        {"mode": "always", "initialDelayMs": 0, "maxDelayMs": 1000, "jitterRatio": 0.0}
    )
    script = [
        ScriptedTurn(error=LlmError("boom")),
        ScriptedTurn(events=[TextDelta("ok"), finish()]),
    ]
    session, provider, _sink, agent = make_agent(tmp_path, script, {"hooks": hooks, "retryPolicy": policy})

    assert await agent.send("go") == "ok"

    assert provider.calls == 2
    record = session.records_of("llm/retry")[0].data
    assert record["mode"] == "always"
    assert record["policyKey"] == '["always",0,1000,0]'
    assert "maxRetries" not in record
    assert record["failure"]["code"] == "PROVIDER_ERROR"


async def test_retry_budget_zero_propagates_the_failure(tmp_path: Path) -> None:
    policy, hooks = _retry_policy_from(
        {
            "mode": "normal",
            "maxRetries": 0,
            "initialDelayMs": 0,
            "maxDelayMs": 1000,
            "retryableCodes": ["SERVER"],
        }
    )
    script = [
        ScriptedTurn(
            error=LlmRequestError(
                "500 server error", provider="fake-provider", status=500, retryable=True, code="SERVER"
            )
        )
    ]
    session, provider, _sink, agent = make_agent(tmp_path, script, {"hooks": hooks, "retryPolicy": policy})

    with pytest.raises(LlmError, match="500 server error"):
        await agent.send("go")

    assert provider.calls == 1
    assert session.records_of("llm/retry") == []
    assert session.records_of("assistant/attempt")


async def test_without_consumer_the_old_failure_behaviour_is_unchanged(tmp_path: Path) -> None:
    hooks = HookService()
    script = [
        ScriptedTurn(
            error=LlmRequestError(
                "500 server error", provider="fake-provider", status=500, retryable=True, code="SERVER"
            )
        )
    ]
    session, provider, _sink, agent = make_agent(tmp_path, script, {"hooks": hooks})

    with pytest.raises(LlmError, match="500 server error"):
        await agent.send("go")

    assert provider.calls == 1
    assert session.records_of("llm/retry") == []
    assert session.records_of("llm/retry-started") == []
