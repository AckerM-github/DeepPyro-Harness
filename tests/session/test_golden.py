from __future__ import annotations

import json
import os
from pathlib import Path

from deeppyro.session.fold import fold
from deeppyro.session.schema import NativeCodec

GOLDEN = Path(__file__).parent.parent / "fixtures" / "native" / "golden-session.jsonl"


def _materialize(tmp_path: Path) -> Path:
    raw = GOLDEN.read_text(encoding="utf-8")
    assert raw.endswith(chr(10))
    assert "\r" not in raw
    lines = raw.splitlines()
    header = json.loads(lines[0])
    if os.name == "nt":
        header["cwd"] = "C:/workspace/project"
    lines[0] = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
    target = tmp_path / f"{header['id']}.jsonl"
    target.write_text(chr(10).join(lines) + chr(10), encoding="utf-8", newline="")
    return target


def test_golden_session_round_trips_byte_for_byte(tmp_path: Path) -> None:
    target = _materialize(tmp_path)
    log = NativeCodec.read(target)

    assert log.header.id == "session-00000000-0000-0000-0000-000000000001"
    assert log.header.preset == "standard"
    assert [event.seq for event in log.events] == list(range(1, 11))
    assert log.events[0].type == "turn.started"
    assert log.events[-1].type == "session.title"

    lines = target.read_text(encoding="utf-8").splitlines()
    assert NativeCodec.encode_header(log.header) == lines[0]
    for index, event in enumerate(log.events, start=1):
        assert NativeCodec.encode_event(event) == lines[index]


def test_golden_session_folds_into_the_expected_view(tmp_path: Path) -> None:
    target = _materialize(tmp_path)
    log = NativeCodec.read(target)
    view = fold(log.header, log.events)

    assert [message.role for message in view.messages] == ["user", "assistant", "tool"]
    assert view.messages[1].text() == "I will read the file."
    calls = view.messages[1].tool_calls()
    assert len(calls) == 1
    assert calls[0].id == "call_0000000000000001"
    assert calls[0].name == "read_file"
    assert view.messages[2].tool_call_id == "call_0000000000000001"
    assert view.messages[2].text() == "hello from notes"
    assert view.last_seq == 10
    assert view.last_message_id == "msg_0000000000000003"
    assert view.state.model is not None
    assert view.state.model.provider == "deepseek"
    assert view.state.model.model == "deepseek-flash"
    assert view.state.preset is not None
    assert view.state.preset.preset == "standard"
    assert view.state.preset.mode == "default"
    assert view.state.title == "Golden session"
    assert view.issues == ()
    assert view.pending_approvals == ()
