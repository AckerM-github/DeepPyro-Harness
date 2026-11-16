"""Approval service: DSH user-approval policy and answerers.

Mirrors dsh-user-approval. The policy is ask or never; "never" rejects before
the approval/request waterfall, so an approval plugin cannot bypass it. When
the policy is ask, the waterfall decides first and an interactive answerer is
the fallback; a missing answerer fails closed (unavailable), it never silently
allows.

Every decision is one-shot (allowed-once) and is audited with log-only
approval/asked and approval/decided records, exactly like DSH.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from deeppyro.api.services import bind_service_type
from deeppyro.core.errors import ConfigError, SessionError
from deeppyro.hooks.types import APPROVAL_REQUEST
from deeppyro.sandbox.types import APPROVALS, Approval, ApprovalRequest

logger = logging.getLogger(__name__)

APPROVAL_POLICY_EVENT = "approval/policy"
APPROVAL_ASKED_EVENT = "approval/asked"
APPROVAL_DECIDED_EVENT = "approval/decided"

#: Hook results from older deeppyro plugins are accepted as aliases.
_OUTCOME_ALIASES: dict[str, str] = {
    "allow": "allowed-once",
    "allowed-once": "allowed-once",
    "deny": "rejected",
    "rejected": "rejected",
    "cancel": "cancelled",
    "cancelled": "cancelled",
}

Answerer = Callable[[ApprovalRequest], Awaitable[bool] | bool]


class ApprovalService:
    """One approval policy plus optional hook service and answerer."""

    def __init__(
        self,
        policy: str = "ask",
        answerer: Answerer | None = None,
        *,
        hooks: Any | None = None,
    ) -> None:
        self._policy = _policy(policy)
        self._answerer = answerer
        self._hooks = hooks

    @property
    def policy(self) -> Approval:
        return self._policy

    def bind_hooks(self, hooks: Any | None) -> None:
        """Attach the hook service lazily (sandbox loads before hooks)."""
        self._hooks = hooks

    def bind_answerer(self, answerer: Answerer | None) -> None:
        """Install or clear the interactive fallback."""
        self._answerer = answerer

    def last_policy(self, session: Any | None = None) -> Approval | None:
        """Return the last approval/policy record of the session, if any."""
        for record in reversed(getattr(session, "records", ()) or ()):
            if getattr(record, "type", None) != APPROVAL_POLICY_EVENT:
                continue
            data = getattr(record, "data", None) or {}
            policy = data.get("policy") if isinstance(data, dict) else None
            if policy in APPROVALS:
                return policy  # type: ignore[return-value]
            raise SessionError(f"invalid approval/policy record: {policy!r}")
        return None

    def effective_policy(self, session: Any | None = None) -> Approval:
        """Session override first, then the configured policy."""
        return self.last_policy(session) or self._policy

    def set_policy(self, session: Any, policy: str) -> Approval:
        """Persist one approval/policy event; a no-op switch writes nothing."""
        resolved = _policy(policy)
        if self.last_policy(session) == resolved:
            return resolved
        session.append(APPROVAL_POLICY_EVENT, {"policy": resolved})
        return resolved

    async def request(
        self,
        request: ApprovalRequest,
        *,
        session: Any | None = None,
        agent: Any | None = None,
        signal: Any | None = None,
    ) -> str:
        """Resolve one approval request into a DSH outcome string."""
        if _is_set(signal):
            return "cancelled"
        policy = self.effective_policy(session)
        if policy == "never":
            # DSH rejects before the waterfall: a prepended listener must not be
            # able to approve while approval prompts are disabled.
            return "rejected"
        approval_id = uuid.uuid4().hex
        asked = {
            "id": approval_id,
            "toolName": request.tool,
        }
        if request.call_id:
            asked["callId"] = request.call_id
        reason = request.reason or request.summary
        if reason:
            asked["reason"] = reason
        # DSH records both halves of an approval; it does not dispatch them on
        # the bus. The record is the audit trail the sandbox policy promises.
        _record(session, APPROVAL_ASKED_EVENT, asked)
        outcome = await self._decide(
            request, policy=policy, agent=agent, signal=signal, approval_id=approval_id
        )
        _record(session, APPROVAL_DECIDED_EVENT, {"id": approval_id, "outcome": outcome})
        return outcome

    async def _decide(
        self,
        request: ApprovalRequest,
        *,
        policy: Approval,
        agent: Any | None,
        signal: Any | None,
        approval_id: str,
    ) -> str:
        outcome = await self._from_waterfall(
            request, policy=policy, agent=agent, signal=signal, approval_id=approval_id
        )
        if outcome is not None:
            return outcome
        if _is_set(signal):
            return "cancelled"
        answerer = self._answerer
        if answerer is None:
            return "unavailable"
        try:
            granted = bool(await _maybe_await(answerer(request)))
        except Exception:
            logger.exception("approval answerer failed")
            return "unavailable"
        if _is_set(signal):
            return "cancelled"
        return "allowed-once" if granted else "rejected"

    async def _from_waterfall(
        self,
        request: ApprovalRequest,
        *,
        policy: Approval,
        agent: Any | None,
        signal: Any | None,
        approval_id: str,
    ) -> str | None:
        hooks = self._hooks
        if hooks is None or not (hooks.has(APPROVAL_REQUEST) or bool(getattr(hooks, "observed", False))):
            return None
        payload = {
            "id": approval_id,
            "request": request,
            "approval": {"id": approval_id, "toolName": request.tool, "callId": request.call_id},
            "agent": agent,
            "policy": policy,
            "signal": signal,
        }
        try:
            raw = await hooks.waterfall(APPROVAL_REQUEST, payload, fallback="unavailable")
        except Exception:
            logger.exception("approval/request waterfall failed")
            return "unavailable"
        if raw is None or raw in ("ask", "unavailable"):
            return None
        mapped = _OUTCOME_ALIASES.get(raw) if isinstance(raw, str) else None
        if mapped is None:
            # DSH turns an unknown listener result into unavailable, not an error.
            return "unavailable"
        return mapped


def _record(session: Any | None, event: str, payload: dict[str, Any]) -> None:
    """Append an audit record, tolerating sessions that cannot record.

    The approval service runs under a sandbox gate that may be probed without a
    session (unit tests, dry runs); a missing recorder must not turn a policy
    decision into an exception.
    """
    append = getattr(session, "append", None)
    if not callable(append):
        return
    try:
        append(event, payload)
    except Exception:  # auditing never blocks a decision
        logger.exception("failed to record %s", event)


def _policy(value: Any) -> Approval:
    if value not in APPROVALS:
        raise ConfigError(f"invalid approval policy: {value!r}")
    return value  # type: ignore[return-value]


def _is_set(signal: Any | None) -> bool:
    checker = getattr(signal, "is_set", None)
    if not callable(checker):
        return False
    return bool(checker())


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# Typed contract for consumers such as headless.pin_permission; discovery through
# provide() alone would depend on plugin import order.
bind_service_type(ApprovalService, "approval")

__all__ = ["APPROVAL_POLICY_EVENT", "Answerer", "ApprovalService"]
