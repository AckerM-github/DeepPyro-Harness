"""Exception hierarchy shared across deeppyro."""

from __future__ import annotations


class DeeppyroError(Exception):
    """Base class for all deeppyro errors."""


class ConfigError(DeeppyroError):
    """Invalid or missing configuration."""


class SessionError(DeeppyroError):
    """Session log is corrupt or an append violated the wire contract."""


class LlmError(DeeppyroError):
    """Provider/transport failure."""

    def __init__(self, message: str, *, provider: str | None = None, status: int | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable


class ToolError(DeeppyroError):
    """Tool raised a user-visible failure; converted into an error tool result."""


class PermissionDenied(DeeppyroError):
    """A sandbox/permission gate denied the operation."""


class AbortedError(DeeppyroError):
    """Operation cancelled cooperatively."""
