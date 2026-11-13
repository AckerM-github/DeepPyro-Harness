"""Errors raised by the skills subsystem."""

from __future__ import annotations

from deeppyro.core.errors import DeeppyroError


class SkillError(DeeppyroError):
    """Base class for skill discovery and lookup failures."""


class SkillParseError(SkillError):
    """A skill file has invalid frontmatter or an invalid body."""


class SkillNotFoundError(SkillError):
    """The requested skill is not in the current catalog."""


__all__ = ["SkillError", "SkillNotFoundError", "SkillParseError"]
