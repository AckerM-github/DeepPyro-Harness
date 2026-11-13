"""Errors raised by the agent team service."""

from __future__ import annotations

from deeppyro.core.errors import DeeppyroError


class TeamError(DeeppyroError):
    """A team operation (spawn, message, wait, task board) could not be completed."""


__all__ = ["TeamError"]
