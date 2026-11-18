"""Feature flag selecting the native TUI runtime path.

The v4 runtime stays the default. The native path is opt-in with the
DEEPPYRO_TUI_NATIVE environment variable, so the migration lands additively:
one flag selects which session/agent seam the TUI boots, and both paths keep
the same widgets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Environment variable that opts the TUI into the native runtime path.
TUI_NATIVE_ENV = "DEEPPYRO_TUI_NATIVE"

#: Values that enable the native path (case insensitive).
TRUTHY = frozenset({"1", "true", "yes", "on"})


def tui_native_enabled(flag: bool | None = None, *, env: Mapping[str, str] | None = None) -> bool:
    """Whether the TUI should boot the native runtime path.

    An explicit flag wins over the environment. Anything unrecognized keeps the
    v4 default, so a typo never silently moves a user onto the experimental
    path.
    """
    if flag is not None:
        return bool(flag)
    source = os.environ if env is None else env
    value = str(source.get(TUI_NATIVE_ENV, "") or "").strip().lower()
    return value in TRUTHY


__all__ = ["TRUTHY", "TUI_NATIVE_ENV", "tui_native_enabled"]
