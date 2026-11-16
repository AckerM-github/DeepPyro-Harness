"""Public kernel value types.

``FiberState`` describes the lifecycle of one mounted plugin. It lives in the
API package (rather than in ``deeppyro.kernel``) so plugin code can import it
without depending on the kernel implementation, and so ``deeppyro.kernel.fiber``
can import this enum without creating a cycle.
"""

from __future__ import annotations

import enum

__all__ = ["FiberState"]


class FiberState(enum.IntEnum):
    """Lifecycle states of a mounted plugin fiber."""

    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5
