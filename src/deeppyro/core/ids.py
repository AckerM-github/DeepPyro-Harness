"""Identifier helpers.

DSH session ids look like `session-<uuid4>` inside directories named by cwd hash;
we generate the same shape so logs remain recognizable to DSH tooling.
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
