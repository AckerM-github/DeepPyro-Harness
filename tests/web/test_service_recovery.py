"""Web host recovery after an unexpected agent-loop failure."""

from __future__ import annotations

from types import SimpleNamespace

from deeppyro.web.events import ErrorEvent, StatusEvent, StatusPhase
from deeppyro.web.service import WebSessionHost


class _FailingHost:
    def __init__(self) -> None:
        self.restored = 0

    async def send(self, text: str) -> None:
        raise RuntimeError("boom")

    def restore(self) -> bool:
        self.restored += 1
        return True


class _Journal:
    def __init__(self) -> None:
        self.events: list[object] = []

    def publish(self, event: object) -> None:
        self.events.append(event)


class _Approvals:
    def cancel_session(self, session_id: str) -> None:
        return None


async def test_unexpected_failure_repairs_the_open_turn() -> None:
    host = object.__new__(WebSessionHost)
    host._journal = _Journal()
    host._approvals = _Approvals()
    inner = _FailingHost()
    binding = SimpleNamespace(session_id="session-1", host=inner)

    await host._run_turn(binding, "hello")

    assert inner.restored == 1
    errors = [event for event in host._journal.events if isinstance(event, ErrorEvent)]
    statuses = [event for event in host._journal.events if isinstance(event, StatusEvent)]
    assert [event.message for event in errors] == ["RuntimeError: boom"]
    assert [event.phase for event in statuses] == [StatusPhase.DONE]
