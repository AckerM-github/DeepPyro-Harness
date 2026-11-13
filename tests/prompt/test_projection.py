"""Tests for the pure system-prompt and runtime-context projections."""

from __future__ import annotations

from deeppyro.core.wire import LogRecord, Message, ReplaceOp
from deeppyro.prompt.projection import (
    CLEARED,
    SOURCE,
    RuntimeContextProjection,
    SystemPromptProjection,
    create_runtime_context_message,
    create_system_message,
)

APPEND = "append"


def _content(text: str) -> list[dict[str, str]]:
    return [] if text == "" else [{"type": "text", "text": text}]


def system_record(
    seq: int,
    text: str,
    *,
    message_id: str | None = None,
    op: str | ReplaceOp = APPEND,
    source_event_seqs: list[int] | None = None,
) -> LogRecord:
    """Build a durable system/message record with the real wire data shape."""
    data = {
        "turn": 1,
        "step": 1,
        "message": {
            "id": message_id or f"msg_system_{seq}",
            "role": "system",
            "content": _content(text),
            "source": {"kind": "system-prompt"},
        },
    }
    return LogRecord(
        type="system/message",
        seq=seq,
        time=1_700_000_000_000 + seq,
        data=data,
        surfaceOp=op,
        sourceEventSeqs=source_event_seqs,
    )


def user_record(
    seq: int,
    text: str,
    *,
    source: dict[str, object] | None = None,
    op: str | ReplaceOp = APPEND,
    source_event_seqs: list[int] | None = None,
) -> LogRecord:
    """Build a durable user/message record (the record data IS the message)."""
    data = {
        "id": f"msg_user_{seq}",
        "role": "user",
        "content": _content(text),
        "source": {"kind": "user"} if source is None else source,
    }
    return LogRecord(
        type="user/message",
        seq=seq,
        time=1_700_000_000_000 + seq,
        data=data,
        surfaceOp=op,
        sourceEventSeqs=source_event_seqs,
    )


def runtime_record(seq: int, text: str, **kwargs: object) -> LogRecord:
    return user_record(seq, text, source={"kind": SOURCE}, **kwargs)


def replace_op(target: int) -> ReplaceOp:
    return ReplaceOp(op="replace", startSeq=target, endSeq=target)


def assert_replace(update: object, seq: int, text: str) -> None:
    from deeppyro.prompt.projection import ProjectionUpdate

    assert isinstance(update, ProjectionUpdate)
    assert update.kind == "replace"
    assert update.replace == [(seq, text)]
    assert update.message is not None
    assert update.message.text() == text


# ---------------------------------------------------------------------------
# SystemPromptProjection
# ---------------------------------------------------------------------------


def test_first_projection_appends_system_prompt() -> None:
    updates = SystemPromptProjection([]).project("hello", in_history=True, starts_series=True)
    assert len(updates) == 1
    update = updates[0]
    assert update.kind == "append"
    assert update.replace == []
    assert isinstance(update.message, Message)
    assert update.message.role == "system"
    assert update.message.text() == "hello"
    assert update.message.source == {"kind": "system-prompt"}
    assert update.message.id


def test_first_empty_projection_reserves_head_node() -> None:
    updates = SystemPromptProjection([]).project("", in_history=False, starts_series=False)
    assert [update.kind for update in updates] == ["append"]
    assert updates[0].message is not None
    assert updates[0].message.content == []
    assert updates[0].message.text() == ""


def test_same_prompt_on_series_start_is_noop() -> None:
    records = [system_record(1, "hello")]
    assert SystemPromptProjection(records).project("hello", in_history=True, starts_series=True) == []


def test_in_history_update_appends_keeping_cached_prefix() -> None:
    records = [system_record(1, "old")]
    updates = SystemPromptProjection(records).project("new", in_history=True, starts_series=False)
    assert [update.kind for update in updates] == ["append"]
    assert updates[0].message is not None
    assert updates[0].message.text() == "new"


def test_in_history_latest_match_is_noop() -> None:
    records = [system_record(1, "A"), system_record(2, "B")]
    assert SystemPromptProjection(records).project("B", in_history=True, starts_series=False) == []


def test_non_history_replaces_head_and_clears_tail() -> None:
    records = [system_record(1, "A"), system_record(2, "B")]
    updates = SystemPromptProjection(records).project("C", in_history=False, starts_series=False)
    assert len(updates) == 2
    assert_replace(updates[0], 2, "")
    assert_replace(updates[1], 1, "C")


def test_non_history_with_unchanged_head_only_clears_tail() -> None:
    records = [system_record(1, "A"), system_record(2, "B")]
    updates = SystemPromptProjection(records).project("A", in_history=False, starts_series=False)
    assert len(updates) == 1
    assert_replace(updates[0], 2, "")


def test_series_start_replaces_when_latest_differs() -> None:
    records = [system_record(1, "A"), system_record(2, "B")]
    updates = SystemPromptProjection(records).project("C", in_history=True, starts_series=True)
    assert len(updates) == 2
    assert_replace(updates[0], 2, "")
    assert_replace(updates[1], 1, "C")


def test_series_start_noop_when_latest_non_head_matches() -> None:
    records = [system_record(1, "A"), system_record(2, "B")]
    assert SystemPromptProjection(records).project("B", in_history=True, starts_series=True) == []


def test_empty_rendered_clears_every_non_empty_node() -> None:
    records = [system_record(1, "A"), system_record(2, "B")]
    updates = SystemPromptProjection(records).project("", in_history=True, starts_series=False)
    assert len(updates) == 2
    assert_replace(updates[0], 2, "")
    assert_replace(updates[1], 1, "")


def test_empty_head_does_not_emit_redundant_replace() -> None:
    records = [system_record(1, ""), system_record(2, "B")]
    updates = SystemPromptProjection(records).project("", in_history=True, starts_series=False)
    assert len(updates) == 1
    assert_replace(updates[0], 2, "")


def test_system_nodes_are_surface_order_and_ignore_other_records() -> None:
    records = [
        user_record(1, "hello"),
        system_record(2, "S2"),
        user_record(3, "again"),
        system_record(4, "S4"),
    ]
    projection = SystemPromptProjection(records)
    assert [node.seq for node in projection.system_nodes()] == [2, 4]
    assert [node.type for node in projection.system_nodes()] == ["system/message", "system/message"]


def test_system_nodes_honor_surface_replacement() -> None:
    records = [
        system_record(1, "old"),
        system_record(2, "new", op=replace_op(1), source_event_seqs=[1]),
    ]
    projection = SystemPromptProjection(records)
    assert [node.seq for node in projection.system_nodes()] == [2]
    assert projection.project("new", in_history=True, starts_series=True) == []


def test_system_text_concatenates_multiple_text_parts() -> None:
    record = system_record(1, "ignored")
    record.data["message"]["content"] = [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
    ]
    assert SystemPromptProjection([record]).project("ab", in_history=True, starts_series=True) == []


def test_create_system_message_has_fresh_ids_and_source() -> None:
    first = create_system_message("one")
    second = create_system_message("one")
    assert first.id != second.id
    assert first.role == "system"
    assert first.source == {"kind": "system-prompt"}
    assert first.text() == "one"


# ---------------------------------------------------------------------------
# RuntimeContextProjection
# ---------------------------------------------------------------------------


def test_runtime_first_projection_appends_plain_snapshot() -> None:
    update = RuntimeContextProjection([]).project("runtime A", None)
    assert update is not None
    assert update.kind == "append"
    assert update.replace == []
    assert isinstance(update.message, Message)
    assert update.message.role == "user"
    assert update.message.text() == "runtime A"
    assert update.message.source == {"kind": SOURCE}
    assert update.message.id


def test_runtime_first_projection_keeps_sections_source() -> None:
    sections: list[dict[str, object]] = [{"name": "time", "text": "now"}]
    update = RuntimeContextProjection([]).project("ctx", sections)
    assert update is not None
    assert update.message is not None
    assert update.message.source == {
        "kind": SOURCE,
        "form": "snapshot",
        "sections": [{"name": "time", "text": "now"}],
    }
    sections[0]["text"] = "mutated"
    assert update.message.source is not None
    assert update.message.source["sections"] == [{"name": "time", "text": "now"}]


def test_runtime_same_snapshot_is_noop() -> None:
    records = [runtime_record(1, "ctx")]
    projection = RuntimeContextProjection(records)
    assert projection.project("ctx", None) is None
    assert projection.project("ctx", []) is None


def test_runtime_empty_current_without_history_is_noop() -> None:
    assert RuntimeContextProjection([]).project("", None) is None


def test_runtime_empty_current_appends_cleared_once() -> None:
    records = [runtime_record(1, "ctx")]
    projection = RuntimeContextProjection(records)
    update = projection.project("", None)
    assert update is not None
    assert update.message is not None
    assert update.message.text() == CLEARED
    assert update.message.source == {"kind": SOURCE}

    committed = [*records, user_record(2, CLEARED, source={"kind": SOURCE})]
    assert RuntimeContextProjection(committed).project("", None) is None


def test_runtime_replacement_clears_retained_snapshot() -> None:
    records = [
        runtime_record(1, "ctx"),
        user_record(2, "summary", source={"kind": "compact"}, op=replace_op(1), source_event_seqs=[1]),
    ]
    projection = RuntimeContextProjection(records)
    assert projection.retained is None
    cleared = projection.project("", None)
    assert cleared is not None
    assert cleared.message is not None
    assert cleared.message.text() == CLEARED
    again = projection.project("ctx", None)
    assert again is not None
    assert again.message is not None
    assert again.message.text() == "ctx"


def test_runtime_newest_surface_snapshot_wins() -> None:
    records = [runtime_record(1, "old"), runtime_record(2, "new")]
    projection = RuntimeContextProjection(records)
    assert projection.project("new", None) is None
    update = projection.project("old", None)
    assert update is not None
    assert update.message is not None
    assert update.message.text() == "old"


def test_runtime_refresh_after_shadowing() -> None:
    records = [runtime_record(1, "ctx")]
    projection = RuntimeContextProjection(records)
    assert projection.project("ctx", None) is None
    records.append(
        user_record(2, "summary", source={"kind": "compact"}, op=replace_op(1), source_event_seqs=[1])
    )
    projection.refresh()
    assert projection.retained is None
    update = projection.project("ctx", None)
    assert update is not None


def test_runtime_ignores_non_runtime_user_messages() -> None:
    records = [user_record(1, "hello"), runtime_record(2, "ctx")]
    projection = RuntimeContextProjection(records)
    assert projection.project("ctx", None) is None
    update = projection.project("", None)
    assert update is not None
    assert update.message is not None
    assert update.message.text() == CLEARED


def test_create_runtime_context_message_sections_are_copied() -> None:
    sections: list[dict[str, object]] = [{"name": "cwd", "text": "/tmp"}]
    message = create_runtime_context_message("ctx", sections)
    sections.append({"name": "model", "text": "x"})
    assert message.source is not None
    assert message.source["sections"] == [{"name": "cwd", "text": "/tmp"}]


def test_prompt_package_exports_projection_api() -> None:
    from deeppyro import prompt

    for name in (
        "RUNTIME_CONTEXT_CLEARED",
        "RUNTIME_CONTEXT_SOURCE",
        "ProjectionAction",
        "ProjectionUpdate",
        "RetainedSnapshot",
        "RuntimeContextProjection",
        "SystemPromptProjection",
        "create_runtime_context_message",
        "create_system_message",
    ):
        assert hasattr(prompt, name), name
