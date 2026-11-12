"""Session v4 log: models, append validation, derivation, fork, store.

Public surface (mirrors docs/spec/INTERFACES.md section 4)::

    from deeppyro.session import SessionStore, Session, SessionInfo, derive_messages

The durable log is the only source of truth: :meth:'Session.append' validates the
event against the wire contract, folds it into the canonical surface, and only
then writes one compact JSON line (flushed and fsynced) before returning its seq.
"""

from __future__ import annotations

from deeppyro.session.derive import APPEND, REPLACE, SKIP, derive_messages, event_message, fold_surface
from deeppyro.session.events import (
    ENVELOPE_KEYS,
    HEADER_OPTIONAL_KEYS,
    HEADER_REQUIRED_KEYS,
    KNOWN_EVENT_TYPES,
    MESSAGE_EVENT_TYPES,
    MESSAGE_ROLE_BY_TYPE,
    PROJECTION_EVENTS,
    SURFACE_EVENTS,
    decode_source_event_seqs,
    encode_source_event_seqs,
    normalize_event,
    validate_header,
)
from deeppyro.session.fork import (
    CLOSER_TEXT,
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    SessionForkError,
    build_fork_seed,
    fork_session,
    open_turn_closers,
)
from deeppyro.session.layout import (
    LOG_BASENAME,
    LOG_SUFFIXES,
    discover_session_files,
    encode_segment,
    is_log_file,
    log_filename,
    project_key,
    session_file_path,
)
from deeppyro.session.reader import SessionRead, decompress_zstd_frames, read_header, read_session_file
from deeppyro.session.session import Session
from deeppyro.session.store import SessionInfo, SessionStore
from deeppyro.session.writer import (
    append_events,
    encode_event_line,
    encode_header_line,
    event_to_dict,
    write_new_session,
)

__all__ = [
    "APPEND",
    "CLOSER_TEXT",
    "ENVELOPE_KEYS",
    "HEADER_OPTIONAL_KEYS",
    "HEADER_REQUIRED_KEYS",
    "KNOWN_EVENT_TYPES",
    "LOG_BASENAME",
    "LOG_SUFFIXES",
    "MESSAGE_EVENT_TYPES",
    "MESSAGE_ROLE_BY_TYPE",
    "PROJECTION_EVENTS",
    "REPLACE",
    "SKIP",
    "SURFACE_EVENTS",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
    "Session",
    "SessionForkError",
    "SessionInfo",
    "SessionRead",
    "SessionStore",
    "append_events",
    "build_fork_seed",
    "decode_source_event_seqs",
    "decompress_zstd_frames",
    "derive_messages",
    "discover_session_files",
    "encode_event_line",
    "encode_header_line",
    "encode_segment",
    "encode_source_event_seqs",
    "event_message",
    "event_to_dict",
    "fold_surface",
    "fork_session",
    "is_log_file",
    "log_filename",
    "normalize_event",
    "open_turn_closers",
    "project_key",
    "read_header",
    "read_session_file",
    "session_file_path",
    "validate_header",
    "write_new_session",
]
