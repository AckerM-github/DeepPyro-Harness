"""Native session runtime.

Session logs are native JSONL files owned by :mod:`deeppyro.session.schema`.
This package used to re-export the v4 append-log stack (Session, SessionStore,
derive/reader/writer/fork, ...); that stack was deleted in 0.3.0, so the package
now exposes only the native entry points.  Everything else lives in the native
submodules and is imported directly from them:

    schema, errors, fold, native_control, native_index, native_query, native_fork
"""

from __future__ import annotations

from deeppyro.session.native_control import NewSession
from deeppyro.session.native_index import NativeSessionInfo
from deeppyro.session.schema import NativeSession

__all__ = ["NativeSession", "NativeSessionInfo", "NewSession"]
