"""Argument validation helpers shared by the built-in tools.

Tools receive already JSON-decoded arguments from the pipeline and must reject
malformed calls with :class:`deeppyro.core.errors.ToolError` so the pipeline can
report them to the model as a normal error result.
"""

from __future__ import annotations

from typing import Any

from deeppyro.core.errors import ToolError

_MISSING = object()


def require_str(
    args: dict[str, Any],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    """Return a required non-empty string argument."""
    value = args.get(key, _MISSING)
    if value is _MISSING:
        raise ToolError(f"{key} is required")
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string")
    if not allow_empty and not value.strip():
        raise ToolError(f"{key} must be a non-empty string")
    return value


def optional_str(args: dict[str, Any], key: str, default: str | None = None) -> str | None:
    """Return an optional string argument (empty strings are rejected)."""
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ToolError(f"{key} must be a string")
    if not value.strip():
        raise ToolError(f"{key} must be a non-empty string")
    return value


def optional_bool(args: dict[str, Any], key: str, default: bool = False) -> bool:
    """Return an optional boolean argument."""
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolError(f"{key} must be a boolean")
    return value


def optional_int(
    args: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Return an optional integer argument with optional bounds."""
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{key} must be an integer")
    if minimum is not None and value < minimum:
        raise ToolError(f"{key} must be greater than or equal to {minimum}")
    if maximum is not None and value > maximum:
        raise ToolError(f"{key} must be less than or equal to {maximum}")
    return value


def optional_str_list(args: dict[str, Any], key: str) -> list[str] | None:
    """Return an optional list-of-strings argument, or None when absent."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        raise ToolError(f"{key} must be an array of strings, not a string")
    if not isinstance(value, list):
        raise ToolError(f"{key} must be an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ToolError(f"{key} must be an array of strings")
        result.append(item)
    return result


def as_object(value: Any, *, name: str) -> dict[str, Any]:
    """Ensure a JSON value is an object."""
    if not isinstance(value, dict):
        raise ToolError(f"{name} must be an object")
    return value
