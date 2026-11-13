"""Shared helpers for projection tests."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from deeppyro.session.session import Session
from deeppyro.session.store import SessionStore

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"
SESSIONS_DIR = FIXTURES_DIR / "sessions"

requires_fixtures = pytest.mark.skipif(not SESSIONS_DIR.is_dir(), reason="fixture sessions are not installed")


def resolve_fixture_file(prefix: str) -> Path:
    """Resolve one fixture session file from a full id or a unique prefix."""
    patterns = (prefix + "*.jsonl", "session-" + prefix + "*.jsonl")
    matches: dict[Path, None] = {}
    for pattern in patterns:
        for path in sorted(SESSIONS_DIR.glob(pattern)):
            matches[path] = None
    found = sorted(matches)
    if not found:
        raise FileNotFoundError(f"no fixture session file matches {prefix!r}")
    if len(found) > 1:
        names = ", ".join(path.name for path in found)
        raise ValueError(f"fixture prefix {prefix!r} is ambiguous: {names}")
    return found[0]


def open_fixture(tmp_path: Path, prefix: str) -> Session:
    """Copy a fixture log into an isolated store and open it as a Session."""
    source = resolve_fixture_file(prefix)
    root = Path(tmp_path) / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, root / source.name)
    return SessionStore(root).open(source.stem)


def make_session(tmp_path: Path, *, root_name: str = "sessions") -> Session:
    """Create an empty flat session under an isolated store root."""
    root = Path(tmp_path) / root_name
    return SessionStore(root).create(Path(tmp_path))


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def user_message_data(message_id: str = "msg_user", text: str = "hello") -> dict[str, Any]:
    return {
        "id": message_id,
        "role": "user",
        "content": [text_part(text)],
        "source": {"kind": "user"},
    }


def system_message_data(message_id: str = "msg_system", text: str = "system prompt") -> dict[str, Any]:
    return {
        "turn": 1,
        "step": 1,
        "message": {
            "id": message_id,
            "role": "system",
            "content": [text_part(text)],
            "source": {"kind": "system-prompt"},
        },
    }


def append_user_message(session: Session, message_id: str = "msg_user", text: str = "hello") -> int:
    return session.append("user/message", user_message_data(message_id, text))


def append_system_prompt(session: Session, text: str = "system prompt") -> int:
    return session.append("system/message", system_message_data(text=text))


def append_turn(session: Session, turn: int = 1) -> None:
    session.append("turn/start", {"turn": turn})
    session.append("step/start", {"turn": turn, "step": 1})
