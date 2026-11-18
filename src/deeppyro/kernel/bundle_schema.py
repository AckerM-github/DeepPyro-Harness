"""Pydantic schema for bundle.toml documents.

The loader validates every raw bundle document, include file and nested group
row against these models before composition builds PluginRow objects.  The
schema owns the shape of a bundle: known keys only, scalar types, the
api_version gate and recursively nested group rows.  Semantics that need the
file system (include files) or cross-row merge rules stay in
deeppyro.kernel.composition.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from deeppyro.api import API_VERSION

__all__ = [
    "BUNDLE_KEYS",
    "DIGEST_PATTERN",
    "DIGEST_PREFIX",
    "ROW_KEYS",
    "SIGNATURE_PATTERN",
    "SIGNATURE_PREFIX",
    "BundleDocument",
    "BundleMeta",
    "RequirementEntry",
    "RowEntry",
    "format_validation_error",
]

#: Keys accepted in one [[plugins]] row.
ROW_KEYS = frozenset(
    {
        "id",
        "plugin",
        "name",
        "config",
        "disabled",
        "before",
        "after",
        "critical",
        "isolate",
        "intercept",
        "group",
        "rows",
        "include",
    }
)
#: Prefix of a pinned bundle content digest: ``sha256:`` plus 64 lowercase
#: hex characters.  The digest covers the raw bytes of bundle.toml with the
#: declared digest value itself replaced by an all-zero placeholder, so pinning
#: a digest does not need a self-referential hash (see
#: docs/guides/bundle-digest.md).
DIGEST_PREFIX = "sha256:"
#: Pattern a declared ``[bundle].digest`` value must match.
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
#: Prefix of an optional bundle signature: ``hmac-sha256:`` plus 64 lowercase
#: hex characters.  The value is an HMAC-SHA256 over the raw bundle.toml bytes
#: with the declared signature value itself replaced by an all-zero
#: placeholder, using a shared secret supplied by the environment at boot (see
#: docs/guides/bundle-signing.md).
SIGNATURE_PREFIX = "hmac-sha256:"
#: Pattern a declared ``[bundle].signature`` value must match.
SIGNATURE_PATTERN = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")

#: Keys accepted in the [bundle] table.
BUNDLE_KEYS = frozenset(
    {
        "id",
        "name",
        "version",
        "api_version",
        "order",
        "critical",
        "requires",
        "digest",
        "signature",
    }
)


def _non_empty(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must be a non-empty string")
    return stripped


class RequirementEntry(BaseModel):
    """One table entry of [bundle].requires."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    version: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _non_empty(value)


class BundleMeta(BaseModel):
    """The [bundle] table of one bundle file."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    name: str | None = None
    version: str | None = None
    api_version: int = API_VERSION
    order: int = 0
    critical: bool = False
    requires: list[str | RequirementEntry] = Field(default_factory=list)
    #: Optional pin of the bundle.toml raw bytes (see docs/guides/bundle-digest.md).
    digest: str | None = None
    #: Optional HMAC-SHA256 signature of the bundle.toml raw bytes, verified at
    #: boot only when present (see docs/guides/bundle-signing.md).
    signature: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _non_empty(value)

    @field_validator("digest")
    @classmethod
    def _check_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not DIGEST_PATTERN.fullmatch(normalized):
            raise ValueError("digest must be 'sha256:' followed by 64 lowercase hex characters")
        return normalized

    @field_validator("signature")
    @classmethod
    def _check_signature(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not SIGNATURE_PATTERN.fullmatch(normalized):
            raise ValueError("signature must be 'hmac-sha256:' followed by 64 lowercase hex characters")
        return normalized

    @field_validator("api_version")
    @classmethod
    def _check_api_version(cls, value: int) -> int:
        if value != API_VERSION:
            raise ValueError(
                f"bundle.api_version {value} targets a different plugin API; this host only "
                f"speaks API v{API_VERSION}, and the v1/v2 plugin API was removed with no "
                "compatibility shim (ADR 0001 section 7)"
            )
        return value


class RowEntry(BaseModel):
    """One raw [[plugins]] row, including nested group rows."""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    plugin: str | None = None
    name: str = ""
    config: dict[str, Any] | None = None
    disabled: bool | None = None
    before: str | None = None
    after: str | None = None
    critical: bool | None = None
    isolate: list[str] = Field(default_factory=list)
    intercept: dict[str, dict[str, Any]] = Field(default_factory=dict)
    group: bool = False
    rows: list[RowEntry] | None = None
    include: str | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        return _non_empty(value)


class BundleDocument(BaseModel):
    """A whole bundle.toml document: one [bundle] table plus ordered rows."""

    model_config = ConfigDict(extra="forbid", strict=True)

    bundle: BundleMeta
    plugins: list[RowEntry] = Field(default_factory=list)


def format_validation_error(exc: ValidationError, *, scope: str = "document") -> str:
    """Render a pydantic validation failure as one composition-style message.

    Args:
        exc: The pydantic validation error to render.
        scope: Where the validated value sits ("document", "bundle" or "row");
            it only disambiguates unknown-key messages at the value's own level.
    """
    errors = exc.errors()
    if not errors:
        return "invalid bundle document"
    return "; ".join(_describe_error(error, scope=scope) for error in errors)


def _describe_error(error: Any, *, scope: str) -> str:
    loc: tuple[Any, ...] = tuple(cast("Iterable[Any]", error.get("loc") or ()))
    error_type = str(error.get("type") or "")
    if error_type == "extra_forbidden":
        key = str(loc[-1]) if loc else "?"
        parent = loc[:-1]
        if not parent:
            if scope == "row":
                return f"unknown plugin row keys: {key}"
            if scope == "bundle":
                return f"unknown [bundle] keys: {key}"
            if scope == "requirement":
                return f"unknown bundle requirement keys: {key}"
            return f"unknown top-level keys: {key}"
        if parent[0] == "bundle":
            if len(parent) >= 2 and parent[1] == "requires":
                return f"unknown bundle requirement keys: {key}"
            return f"unknown [bundle] keys: {key}"
        return f"unknown plugin row keys: {key}"
    message = str(error.get("msg") or "invalid value")
    prefix = "Value error, "
    if message.startswith(prefix):
        message = message[len(prefix) :]
    if loc:
        path = ".".join(str(part) for part in loc)
        return f"{path}: {message}"
    return message
