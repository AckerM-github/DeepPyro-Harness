"""Errors raised by the runtime memory stack."""

from __future__ import annotations

from deeppyro.core.errors import DeeppyroError


class MemoryStoreError(DeeppyroError):
    """Base class for runtime memory failures."""


class MemoryCorruptError(MemoryStoreError):
    """memories.json is unreadable, malformed or uses an unknown version."""


class MemoryCapacityError(MemoryStoreError):
    """A write would push a projection over its hard byte cap."""


class MemoryValidationError(MemoryStoreError):
    """A target, importance, content or selector value is invalid."""


class MemoryEntryNotFoundError(MemoryStoreError):
    """The requested entry does not exist in the given target."""


class MemoryAmbiguousMatchError(MemoryStoreError):
    """A substring selector matches more than one entry."""


class MemoryBackendError(MemoryStoreError):
    """Base class for long-term (backend) memory failures."""


class MemoryBackendCorruptError(MemoryBackendError):
    """The long-term memory database is unreadable, malformed or too new."""


class MemoryBackendUnavailableError(MemoryBackendError):
    """The long-term memory backend cannot be opened or was already closed."""


class MemoryRecordNotFoundError(MemoryBackendError):
    """The requested long-term memory record does not exist."""


__all__ = [
    "MemoryAmbiguousMatchError",
    "MemoryBackendCorruptError",
    "MemoryBackendError",
    "MemoryBackendUnavailableError",
    "MemoryCapacityError",
    "MemoryCorruptError",
    "MemoryEntryNotFoundError",
    "MemoryRecordNotFoundError",
    "MemoryStoreError",
    "MemoryValidationError",
]
