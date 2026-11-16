"""Read a field out of a payload that may be a mapping or an object.

The same record arrives in two shapes at nearly every boundary: the parsed JSON
mapping from disk or from a request body, and the model object built from it.
Both expose the same field names, so boundaries accept either and read through
field_of() instead of repeating
"x.get(name) if isinstance(x, Mapping) else getattr(x, name, None)" everywhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def field_of(payload: object, name: str, default: Any = None) -> Any:
    """Return *name* from *payload*: mapping lookup first, then attribute.

    A missing key and an attribute that is not set both yield *default*, so
    callers do not need to know which shape they were handed.
    """
    if isinstance(payload, Mapping):
        return payload.get(name, default)
    return getattr(payload, name, default)
