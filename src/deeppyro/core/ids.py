"""Identifier helpers.

upstream session ids look like `session-<uuid4>` inside directories named by cwd hash;
we generate the same shape so logs remain recognizable to upstream tooling.
"""

from __future__ import annotations

import time
import uuid


def new_session_id() -> str:
    return f"session-{uuid.uuid4()}"


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:16]}"


def new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_run_id() -> str:
    """Native trace run id (schema 1: run_ plus 16 lowercase hex digits)."""
    return f"run_{uuid.uuid4().hex[:16]}"


def new_turn_id() -> str:
    return f"turn_{uuid.uuid4().hex[:16]}"


def new_step_id() -> str:
    return f"step_{uuid.uuid4().hex[:16]}"


def new_span_id() -> str:
    return f"span_{uuid.uuid4().hex[:16]}"


def new_trace_id() -> str:
    """Trace id: 32 lowercase hex digits, no prefix."""
    return uuid.uuid4().hex
