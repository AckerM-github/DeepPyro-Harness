from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.core.wire import LogRecord
from deeppyro.session.derive import derive_messages

from .golden import assistant_responses, first_session_with_assistant, load_session_records, user_texts

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
if not (_FIXTURES / "sessions").is_dir():
    pytest.skip(
        "real DSH fixtures not present; run: node tools/gen_fixtures.mjs",
        allow_module_level=True,
    )


def test_golden_helpers_load_a_real_session() -> None:
    path = first_session_with_assistant("fixtures/sessions")
    header, records = load_session_records(path)
    assert header["type"] == "session"
    assert records
    responses = assistant_responses(records)
    assert responses, path
    assert user_texts(records), path


def test_every_scripted_response_starts_with_content_or_tool_calls() -> None:
    path = first_session_with_assistant("fixtures/sessions")
    _, records = load_session_records(path)
    for response in assistant_responses(records):
        assert response.text or response.reasoning or response.tool_calls
        if response.tool_calls:
            assert response.finish == "tool-calls"
            for call_id, name, args in response.tool_calls:
                assert call_id and name
                if args:
                    import json

                    json.loads(args)


def test_derived_messages_match_recorded_user_texts() -> None:
    path = first_session_with_assistant("fixtures/sessions")
    _, records = load_session_records(path)
    logs = [LogRecord.model_validate(rec) for rec in records]
    messages = derive_messages(logs, strict=False)
    derived_user = [m.text() for m in messages if m.role == "user"]
    recorded = user_texts(records)
    assert derived_user, path
    # derived history may drop inactive/checkpoint user nodes, so only assert the
    # first recorded prompt survives as the first derived user message
    assert recorded[0] in derived_user[0]
