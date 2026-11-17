"""Plugin API version constants.

The API version is a hard gate: a plugin, bundle or plugin.toml that declares a
different api_version is rejected at load time, with a migration hint in the
error.  Session schema versions are separate and are covered by
docs/native/schema.md.
"""

from __future__ import annotations

API_VERSION = 3

__all__ = ["API_VERSION"]
