"""Errors raised by the default subagent layer."""

from __future__ import annotations

from deeppyro.core.errors import DeeppyroError


class SubagentError(DeeppyroError):
    """A subagent run could not be started or completed."""


__all__ = ["SubagentError"]
