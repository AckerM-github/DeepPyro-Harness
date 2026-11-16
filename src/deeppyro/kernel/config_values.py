"""Render plugin config values as plain dicts for kernel bookkeeping.

A plugin may declare its config as a pydantic model, a plain mapping, or
nothing at all. The kernel compares configs to decide whether a hot update is
possible, and re-validates them after a patch, so it needs one canonical shape:
a deep-copied dict of plain Python values.

This is a boundary helper. Past it the value is a dict of Any on purpose: each
plugin owns its own config schema, and the kernel never inspects the fields it
cannot see.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any


def config_dict(value: Any) -> dict[str, Any]:
    """Return a deep-copied dict for a plugin config of any supported shape."""
    if value is None:
        return {}
    dump: Callable[..., Any] | None = getattr(value, "model_dump", None)
    if callable(dump):
        data: Any
        try:
            data = dump(mode="python")
        except TypeError:  # pragma: no cover - very old pydantic models
            data = dump()
        return copy.deepcopy(dict(data))
    if isinstance(value, dict):
        return copy.deepcopy(value)
    return {"value": copy.deepcopy(value)}
