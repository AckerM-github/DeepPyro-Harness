"""Typed errors raised by the native agent loop."""

from __future__ import annotations


class AgentLoopError(RuntimeError):
    """Base class for failures raised by :mod:`deeppyro.agent.loop`."""


class ProviderStreamError(AgentLoopError):
    """The provider stream ended or failed without a usable response."""


class RunCancelledError(AgentLoopError):
    """The run was cancelled through its cancel event."""


class SessionMismatchError(AgentLoopError):
    """The session log and the checkpoint belong to different sessions."""


__all__ = [
    "AgentLoopError",
    "ProviderStreamError",
    "RunCancelledError",
    "SessionMismatchError",
]
