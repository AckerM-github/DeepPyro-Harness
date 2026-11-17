"""Error hierarchy for the offline evaluation and replay layer."""

from __future__ import annotations

from deeppyro.core.errors import DeeppyroError


class EvalError(DeeppyroError):
    """Base class for evaluation and replay failures."""


class EvalValidationError(EvalError):
    """A case, outcome, result or fixture payload violates the eval schema."""


class EvalFixtureError(EvalError):
    """A golden eval fixture cannot be read or parsed."""


class ReplayError(EvalError):
    """A recorded native fixture cannot be replayed."""


__all__ = [
    "EvalError",
    "EvalFixtureError",
    "EvalValidationError",
    "ReplayError",
]
