"""Loopback web approval broker for the native sandbox PermissionGate.

The native sandbox gate answers one-shot approvals through an async answerer.
Because the web UI has no terminal modal, this broker sits between the two: it
turns one ApprovalRequest into an ApprovalPrompt the browser can answer, waits
for that decision on the running event loop, then reports it back to the gate.
Only allow-once exists; deny, expiry, cancellation and shutdown all fail closed.

The broker is in-memory and loop-local: it opens no socket (the HTTP server is
loopback-only and lives elsewhere), spawns no threads and takes no locks. A
prompt is not history: once decided, the host journals the decision into the
native session facts through the notify/on_decided callbacks it wires in. All
state changes happen synchronously on one event loop, so a callback may safely
call decide() -- or close() -- from inside notify() without a deadlock.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from deeppyro.sandbox.types import ApprovalRequest

logger = logging.getLogger(__name__)

#: How long the browser has to answer one approval prompt.
DEFAULT_TIMEOUT_SECONDS = 300.0


class ApprovalDecision(StrEnum):
    """Terminal answer for one approval prompt.

    The values are the snake_case wire spelling shared with browser clients.
    """

    ALLOW = "allow"
    DENY = "deny"
    EXPIRE = "expire"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    """One approval request waiting for a browser answer.

    status is None while the prompt is pending and becomes the terminal
    ApprovalDecision once the broker settles it. snapshots handed out by
    pending() are immutable history: a later decision never mutates them.
    """

    approval_id: str
    session_id: str
    tool: str
    summary: str
    detail: str = ""
    created_at: float = field(default_factory=time.time)
    status: ApprovalDecision | None = None


@dataclass(frozen=True, slots=True)
class _Pending:
    """A prompt paired with the ask() future that is waiting on it."""

    prompt: ApprovalPrompt
    future: asyncio.Future[ApprovalDecision]


class ApprovalBroker:
    """Bridge native sandbox approvals to browser decisions on one event loop."""

    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        notify: Callable[[ApprovalPrompt], None] | None = None,
        on_decided: Callable[[ApprovalPrompt, ApprovalDecision], None] | None = None,
    ) -> None:
        """Create an empty broker.

        timeout_seconds bounds how long ask() waits for a browser answer.
        notify is called with each new prompt before ask() starts waiting.
        on_decided is called exactly once per settled prompt with its final
        status and decision. Both callbacks run on the broker's event loop.
        """
        self._timeout_seconds = float(timeout_seconds)
        self._notify = notify
        self._on_decided = on_decided
        self._pending: dict[str, _Pending] = {}
        self._closed = False

    @property
    def closed(self) -> bool:
        """Whether the broker was shut down and now fails every ask closed."""
        return self._closed

    @property
    def timeout_seconds(self) -> float:
        """Seconds one ask() waits for a browser answer before expiring."""
        return self._timeout_seconds

    async def ask(self, session_id: str, request: ApprovalRequest) -> bool:
        """Ask the browser for one decision; return True only for allow.

        The prompt is registered before notify() runs, so a synchronous
        decide() from the callback still resolves this same ask. Every exit
        path settles the prompt and removes it from pending(), so a timed-out,
        cancelled or shut-down ask never leaves an orphan prompt behind. A
        cancelled turn is answered with a plain "not allowed": the sandbox
        path fails closed and no CancelledError leaks into the approval gate.
        """
        if self._closed:
            return False
        prompt = ApprovalPrompt(
            approval_id=uuid.uuid4().hex,
            session_id=session_id,
            tool=request.tool or request.kind,
            summary=request.summary,
            detail=request.detail,
            created_at=time.time(),
        )
        pending = _Pending(prompt=prompt, future=asyncio.get_running_loop().create_future())
        self._pending[prompt.approval_id] = pending
        try:
            if self._notify is not None:
                try:
                    self._notify(prompt)
                except Exception:
                    logger.exception("approval notify callback failed for %s", prompt.approval_id)
                    self._settle(prompt.approval_id, ApprovalDecision.CANCEL)
                    return False
            decision = await asyncio.wait_for(pending.future, self._timeout_seconds)
        except TimeoutError:
            logger.debug("approval %s expired after %.3gs", prompt.approval_id, self._timeout_seconds)
            self._settle(prompt.approval_id, ApprovalDecision.EXPIRE)
            return False
        except asyncio.CancelledError:
            logger.debug("approval %s cancelled with its turn", prompt.approval_id)
            self._settle(prompt.approval_id, ApprovalDecision.CANCEL)
            return False
        finally:
            self._pending.pop(prompt.approval_id, None)
        return decision is ApprovalDecision.ALLOW

    def decide(self, approval_id: str, decision: ApprovalDecision | str) -> bool:
        """Resolve one waiting prompt; True when this call settled it.

        Repeated calls, unknown ids and prompts already settled by timeout,
        cancellation or close() are no-ops that return False and emit no
        further on_decided callback. A string decision is normalised through
        ApprovalDecision, so an invalid value raises ValueError before any
        state changes.
        """
        return self._settle(approval_id, ApprovalDecision(decision))

    def pending(self, session_id: str | None = None) -> list[ApprovalPrompt]:
        """Snapshot the prompts still waiting, oldest first.

        A session_id narrows the snapshot to one session; None returns every
        prompt. The returned list and its prompts are immutable snapshots.
        """
        return [
            pending.prompt
            for pending in self._pending.values()
            if session_id is None or pending.prompt.session_id == session_id
        ]

    def cancel_session(self, session_id: str) -> int:
        """Cancel every waiting prompt of one session; return how many."""
        approval_ids = [
            pending.prompt.approval_id
            for pending in self._pending.values()
            if pending.prompt.session_id == session_id
        ]
        for approval_id in approval_ids:
            self._settle(approval_id, ApprovalDecision.CANCEL)
        return len(approval_ids)

    def close(self) -> None:
        """Deny every waiting prompt and refuse new ones (host shutdown).

        Idempotent: later calls find no pending prompt. Asking after close()
        returns False without notifying the browser.
        """
        self._closed = True
        for approval_id in list(self._pending):
            self._settle(approval_id, ApprovalDecision.DENY)

    def _settle(self, approval_id: str, decision: ApprovalDecision) -> bool:
        """Resolve one prompt exactly once and report it through on_decided."""
        pending = self._pending.pop(approval_id, None)
        if pending is None:
            return False
        if not pending.future.done():
            pending.future.set_result(decision)
        decided = replace(pending.prompt, status=decision)
        if self._on_decided is not None:
            try:
                self._on_decided(decided, decision)
            except Exception:
                logger.exception("approval on_decided callback failed for %s", approval_id)
        return True


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "ApprovalBroker",
    "ApprovalDecision",
    "ApprovalPrompt",
]
