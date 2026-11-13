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


__all__ = [
    "MemoryAmbiguousMatchError",
    "MemoryCapacityError",
    "MemoryCorruptError",
    "MemoryEntryNotFoundError",
    "MemoryStoreError",
    "MemoryValidationError",
]
