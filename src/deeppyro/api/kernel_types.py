"""Kernel types re-exported through the API boundary.

The cordis port lives under deeppyro._vendor.cordis_port and may only be
imported from the kernel and API packages (see tests/architecture).  Plugins
that need a kernel type import it from here or from deeppyro.api instead of
reaching into the vendored implementation.
"""

from __future__ import annotations

from deeppyro._vendor.cordis_port import FiberState

__all__ = ["FiberState"]
