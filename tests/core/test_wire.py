"""Lead-owned contract tests: every real fixture record must round-trip through core.wire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.core.wire import LogRecord, Message, SessionHeader

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

pytestmark = pytest.mark.skipif(
    not FIXTURES.is_dir(),
    reason="real DSH fixtures not present; run: node tools/gen_fixtures.mjs",
)


def _fixture_sessions() -> list[Path]:
    return sorted((FIXTURES / "sessions").glob("*.jsonl"))


def test_fixtures_exist() -> None:
    assert _fixture_sessions(), "fixtures/sessions is empty; run: node tools/gen_fixtures.mjs"


def test_every_session_header_parses() -> None:
    for path in _fixture_sessions():
        first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        header = SessionHeader.model_validate(first)
        assert header.type == "session"
        assert header.version == 4
        assert header.id
        if header.origin != "subagent":  # subagent sessions use a bare UUID
            assert header.id.startswith("session-")
        assert isinstance(header.createdAt, int)


def test_every_record_parses_and_keeps_unknown_fields() -> None:
    types: set[str] = set()
    for path in _fixture_sessions():
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            raw = json.loads(line)
            rec = LogRecord.model_validate(raw)
            types.add(rec.type)
            assert isinstance(rec.seq, int)
            # extra envelope keys survive (surfaceOp / sourceEventSeqs)
            assert set(raw) <= set(rec.model_dump())
    assert len(types) >= 35, f"expected broad record-type coverage, got {len(types)}"


def test_by_type_samples_are_valid_records() -> None:
    for path in sorted((FIXTURES / "records" / "by-type").glob("*.jsonl")):
        raw = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        LogRecord.model_validate(raw)


def test_message_payloads_parse() -> None:
    """Every message-shaped payload in fixtures validates against Message."""
    seen_roles: set[str] = set()
    counted = 0
    for path in _fixture_sessions():
        for line in path.read_text(encoding="utf-8").splitlines()[1:]:
            raw = json.loads(line)
            data = raw.get("data") or {}
            if raw.get("type") == "user/message":
                msg = data  # user/message stores the message inline, not under data.message
            else:
                msg = data.get("message")
            if not isinstance(msg, dict) or "role" not in msg:
                continue
            message = Message.model_validate(msg)
            seen_roles.add(message.role)
            counted += 1
            for call in message.tool_calls():
                assert isinstance(call.arguments, str)
                json.loads(call.arguments)
    assert counted > 100
    assert {"system", "user", "assistant", "tool", "developer"} <= seen_roles, seen_roles
