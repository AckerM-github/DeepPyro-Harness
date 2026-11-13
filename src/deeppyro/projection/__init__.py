"""Session projection registry and durable projection checkpoints (P1 feature B).

Public API:
    ProjectionDefinition, ProjectionRegistry, ProjectionHandle, ProjectionError
        In-process pure-fold projections with per-session incremental cells.
    inherited_event_count(session)
        Fork cut size (position of the inherited end-seed marker).
    sidecar_path, encode_checkpoint, decode_checkpoint, read_checkpoint,
    write_checkpoint, save_checkpoint, load_checkpoint, ProjectionCheckpoints
        Durable sidecar checkpoints stored next to the session log.

Hosts register definitions up front, then call registry.notify(session, record)
after each session append.  Sidecars are written with save_checkpoint /
ProjectionCheckpoints.save (write-behind) and consumed at session open with
load_checkpoint / ProjectionCheckpoints.load, which replays only the log tail
after the checkpoint.
"""

from __future__ import annotations

from deeppyro.projection.checkpoint import (
    SIDECAR_SUFFIX,
    SIDECAR_VERSION,
    ProjectionCheckpoints,
    decode_checkpoint,
    encode_checkpoint,
    load_checkpoint,
    read_checkpoint,
    save_checkpoint,
    sidecar_path,
    write_checkpoint,
)
from deeppyro.projection.registry import (
    ProjectionDefinition,
    ProjectionError,
    ProjectionHandle,
    ProjectionRegistry,
    inherited_event_count,
)

__all__ = [
    "SIDECAR_SUFFIX",
    "SIDECAR_VERSION",
    "ProjectionCheckpoints",
    "ProjectionDefinition",
    "ProjectionError",
    "ProjectionHandle",
    "ProjectionRegistry",
    "decode_checkpoint",
    "encode_checkpoint",
    "inherited_event_count",
    "load_checkpoint",
    "read_checkpoint",
    "save_checkpoint",
    "sidecar_path",
    "write_checkpoint",
]
