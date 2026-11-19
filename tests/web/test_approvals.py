"""ApprovalBroker: browser decisions bound to native sandbox approval requests.

The broker is the web host's answerer for PermissionGate: each ask() publishes
one prompt, waits for the browser, and fails closed on every non-allow outcome.
These tests drive the broker's own loop directly (no HTTP, no sockets).
"""

from __future__ import annotations

import asyncio

import pytest

from deeppyro.sandbox.types import ApprovalRequest
from deeppyro.web.approvals import (
    DEFAULT_TIMEOUT_SECONDS,
    ApprovalBroker,
    ApprovalDecision,
    ApprovalPrompt,
)


def _request(*, tool: str | None = "shell") -> ApprovalRequest:
    """One realistic sandbox-escalation request."""
    return ApprovalRequest(
        kind="sandbox-escalation",
        summary='Allow this write to run with "danger-full-access" for one call?',
        detail="the target file lives outside the workspace",
        tool=tool,
    )


async def _wait_for_pending(broker: ApprovalBroker, count: int = 1) -> None:
    """Yield to the loop until the broker holds count waiting prompts."""
    for _ in range(1000):
        if len(broker.pending()) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"broker never reached {count} pending prompt(s)")


async def _start_ask(
    broker: ApprovalBroker,
    session_id: str = "session-a",
    *,
    tool: str | None = "shell",
    expected: int = 1,
) -> asyncio.Task[bool]:
    """Start one ask() in the background and wait for its pending prompt."""
    task = asyncio.create_task(broker.ask(session_id, _request(tool=tool)))
    await _wait_for_pending(broker, expected)
    return task


def test_default_timeout_is_five_minutes() -> None:
    broker = ApprovalBroker()
    assert broker.timeout_seconds == DEFAULT_TIMEOUT_SECONDS == 300.0
    assert ApprovalBroker(timeout_seconds=5).timeout_seconds == 5.0
    assert broker.closed is False


async def test_allow_resolves_ask_true() -> None:
    decided: list[tuple[ApprovalPrompt, ApprovalDecision]] = []
    broker = ApprovalBroker(on_decided=lambda prompt, decision: decided.append((prompt, decision)))
    task = await _start_ask(broker)
    prompt = broker.pending()[0]

    assert broker.decide(prompt.approval_id, ApprovalDecision.ALLOW) is True
    assert (await task) is True
    assert broker.pending() == []
    assert len(decided) == 1
    decided_prompt, decision = decided[0]
    assert decision is ApprovalDecision.ALLOW
    assert decided_prompt.approval_id == prompt.approval_id
    assert decided_prompt.status is ApprovalDecision.ALLOW


async def test_deny_resolves_ask_false() -> None:
    decided: list[ApprovalDecision] = []
    broker = ApprovalBroker(on_decided=lambda prompt, decision: decided.append(decision))
    task = await _start_ask(broker)
    prompt = broker.pending()[0]

    assert broker.decide(prompt.approval_id, ApprovalDecision.DENY) is True
    assert (await task) is False
    assert broker.pending() == []
    assert decided == [ApprovalDecision.DENY]


def test_unknown_approval_id_is_a_noop() -> None:
    decided: list[ApprovalDecision] = []
    broker = ApprovalBroker(on_decided=lambda prompt, decision: decided.append(decision))

    assert broker.decide("missing", ApprovalDecision.ALLOW) is False
    assert decided == []
    with pytest.raises(ValueError):
        broker.decide("missing", "maybe")
    assert broker.pending() == []


async def test_timeout_expires_the_prompt() -> None:
    decided: list[ApprovalDecision] = []
    broker = ApprovalBroker(
        timeout_seconds=0.01,
        on_decided=lambda prompt, decision: decided.append(decision),
    )

    assert await broker.ask("session-a", _request()) is False
    assert broker.pending() == []
    assert decided == [ApprovalDecision.EXPIRE]


async def test_cancelled_turn_fails_closed() -> None:
    decided: list[ApprovalDecision] = []
    broker = ApprovalBroker(on_decided=lambda prompt, decision: decided.append(decision))
    task = await _start_ask(broker)

    task.cancel()
    assert (await task) is False
    assert broker.pending() == []
    assert decided == [ApprovalDecision.CANCEL]


async def test_cancel_session_counts_and_cancels_only_that_session() -> None:
    decided: list[ApprovalDecision] = []
    broker = ApprovalBroker(on_decided=lambda prompt, decision: decided.append(decision))
    first = await _start_ask(broker, "session-a")
    second = await _start_ask(broker, "session-a", expected=2)
    other = await _start_ask(broker, "session-b", expected=3)

    assert broker.cancel_session("session-a") == 2
    assert (await first) is False
    assert (await second) is False
    remaining = broker.pending()
    assert [prompt.session_id for prompt in remaining] == ["session-b"]
    assert broker.cancel_session("session-missing") == 0

    broker.decide(remaining[0].approval_id, ApprovalDecision.ALLOW)
    assert (await other) is True
    assert decided == [
        ApprovalDecision.CANCEL,
        ApprovalDecision.CANCEL,
        ApprovalDecision.ALLOW,
    ]


async def test_pending_returns_immutable_snapshots_in_arrival_order() -> None:
    notified: list[ApprovalPrompt] = []
    broker = ApprovalBroker(notify=notified.append)
    first = await _start_ask(broker, "session-a")
    second = await _start_ask(broker, "session-b", tool=None, expected=2)

    snapshot = broker.pending()
    assert [prompt.session_id for prompt in snapshot] == ["session-a", "session-b"]
    assert broker.pending("session-a") == [snapshot[0]]
    assert broker.pending("session-b") == [snapshot[1]]
    assert broker.pending("session-missing") == []
    assert snapshot == notified
    assert snapshot[0].tool == "shell"
    assert snapshot[1].tool == "sandbox-escalation"
    assert snapshot[0].summary == _request().summary
    assert snapshot[0].detail == _request().detail
    assert snapshot[0].status is None
    assert snapshot[0].created_at > 0.0
    assert snapshot[0].approval_id != snapshot[1].approval_id

    broker.decide(snapshot[0].approval_id, ApprovalDecision.ALLOW)
    assert (await first) is True
    assert snapshot[0].status is None
    assert broker.pending() == [snapshot[1]]

    broker.decide(snapshot[1].approval_id, ApprovalDecision.DENY)
    assert (await second) is False


async def test_double_decide_settles_once() -> None:
    decided: list[ApprovalDecision] = []
    broker = ApprovalBroker(on_decided=lambda prompt, decision: decided.append(decision))
    task = await _start_ask(broker)
    approval_id = broker.pending()[0].approval_id

    assert broker.decide(approval_id, ApprovalDecision.ALLOW) is True
    assert broker.decide(approval_id, ApprovalDecision.DENY) is False
    assert (await task) is True
    assert decided == [ApprovalDecision.ALLOW]


async def test_close_denies_pending_and_refuses_new_asks() -> None:
    notified: list[ApprovalPrompt] = []
    decided: list[ApprovalDecision] = []
    broker = ApprovalBroker(
        notify=notified.append,
        on_decided=lambda prompt, decision: decided.append(decision),
    )
    first = await _start_ask(broker, "session-a")
    second = await _start_ask(broker, "session-b", expected=2)

    broker.close()
    assert broker.closed is True
    assert (await first) is False
    assert (await second) is False
    assert broker.pending() == []
    assert decided == [ApprovalDecision.DENY, ApprovalDecision.DENY]

    broker.close()
    assert await broker.ask("session-c", _request()) is False
    assert len(notified) == 2
    assert broker.cancel_session("session-a") == 0


async def test_decide_accepts_wire_decision_strings() -> None:
    broker = ApprovalBroker()
    task = await _start_ask(broker)

    assert broker.decide(broker.pending()[0].approval_id, "allow") is True
    assert (await task) is True


async def test_notify_may_decide_synchronously() -> None:
    decided: list[ApprovalDecision] = []
    broker: ApprovalBroker

    def notify(prompt: ApprovalPrompt) -> None:
        # ask() registers the prompt before notify(), so the callback may answer it.
        assert broker.pending() == [prompt]
        assert broker.decide(prompt.approval_id, ApprovalDecision.ALLOW) is True

    broker = ApprovalBroker(
        notify=notify,
        on_decided=lambda prompt, decision: decided.append(decision),
    )
    assert await broker.ask("session-a", _request()) is True
    assert decided == [ApprovalDecision.ALLOW]
    assert broker.pending() == []


async def test_notify_failure_fails_closed() -> None:
    decided: list[ApprovalDecision] = []

    def notify(prompt: ApprovalPrompt) -> None:
        raise RuntimeError("the event journal is closed")

    broker = ApprovalBroker(
        notify=notify,
        on_decided=lambda prompt, decision: decided.append(decision),
    )
    assert await broker.ask("session-a", _request()) is False
    assert broker.pending() == []
    assert decided == [ApprovalDecision.CANCEL]


async def test_on_decided_failure_keeps_the_decision() -> None:
    def on_decided(prompt: ApprovalPrompt, decision: ApprovalDecision) -> None:
        raise RuntimeError("the sink is gone")

    broker = ApprovalBroker(on_decided=on_decided)
    task = await _start_ask(broker)
    approval_id = broker.pending()[0].approval_id

    assert broker.decide(approval_id, ApprovalDecision.ALLOW) is True
    assert (await task) is True
    assert broker.pending() == []
