"""Errors raised by the agent team service."""

from __future__ import annotations

from deeppyro.core.errors import DeeppyroError


class TeamError(DeeppyroError):
    """A team operation (spawn, message, wait, task board) could not be completed."""


class NativeTeamError(TeamError):
    """A native team operation needs a control-plane feature that is unavailable.

    Raised when the bound services cannot create native child sessions or agent
    hosts (for example a sessions service without native_create). Restore never
    invents state: a member whose child session cannot be opened is marked
    failed with the error message on the roster instead.
    """


__all__ = ["NativeTeamError", "TeamError"]
