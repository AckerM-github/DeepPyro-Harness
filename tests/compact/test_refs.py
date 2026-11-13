"""Tests for stable m references and the ref sidecar."""

from __future__ import annotations

import pathlib

import pytest

from deeppyro.compact.blocks import CompactError
from deeppyro.compact.refs import (
    RefEntry,
    RefStore,
    addressable_messages,
    canonical_ref,
    ensure_refs,
    ref_ordinal,
    resolve_refs,
)
from tests.compact import support


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5, "m00005"),
        ("m5", "m00005"),
        (" M00007 ", "m00007"),
        ("m123456", "m123456"),
    ],
)
def test_canonical_ref_normalizes(raw: object, expected: str) -> None:
    assert canonical_ref(raw) == expected  # type: ignore[arg-type]
    assert ref_ordinal(expected) == int(expected[1:])


@pytest.mark.parametrize("bad", ["m0", "x12", "", 0, -3, True, None, 3.5])
def test_canonical_ref_rejects_garbage(bad: object) -> None:
    with pytest.raises(CompactError) as info:
        canonical_ref(bad)  # type: ignore[arg-type]
    assert info.value.code == "COMPACT_INVALID_REF"


def test_addressable_messages_skip_system_and_placeholders(tmp_path: pathlib.Path) -> None:
    session_store, _root, _blocks, _refs = support.stores(tmp_path)
    session = support.seed_session(session_store)
    addressable = addressable_messages(session.records)
    assert [seq for seq, _message in addressable] == [1, 2, 3, 4, 6, 7]
    assert [message.role for _seq, message in addressable] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "user",
    ]


def test_resolve_refs_pins_ordinals_without_writing(tmp_path: pathlib.Path) -> None:
    session_store, _root, _blocks, refs = support.stores(tmp_path)
    session = support.seed_session(session_store)
    resolution = resolve_refs(session, ["m00001", "m00003"], refs)
    assert [(entry.ref, entry.seq) for entry in resolution.resolved] == [
        ("m00001", 1),
        ("m00003", 3),
    ]
    assert [entry.seq for entry in resolution.pending] == [1, 3]
    assert not refs.path(session.session_id).exists()

    ensure_refs(session, ["m00001", "m00003"], refs)
    assert [entry.ref for entry in refs.entries(session.session_id)] == ["m00001", "m00003"]
    assert resolve_refs(session, ["m00001", "m00003"], refs).pending == ()
    assert [entry.ref for entry in refs.entries(session.session_id)] == ["m00001", "m00003"]
    again = resolve_refs(session, ["m00001"], refs)
    assert again.pending == ()


async def test_frozen_ref_does_not_move_after_surface_changes(tmp_path: pathlib.Path) -> None:
    tool, session_store, _blocks, refs = support.make_tool(tmp_path)
    session = support.seed_session(session_store)
    ensure_refs(session, ["m00001", "m00002"], refs)
    result = await tool.run({"content": "m00001–m00002 topic\n\nshort summary"}, support.make_ctx(session))
    assert not result.is_error, result.text
    assert session.derive_messages()[1].source["kind"] == "compact-checkpoint"
    after = resolve_refs(session, ["m00001", "m00002"], refs)
    assert [entry.seq for entry in after.resolved] == [1, 2]
    assert after.resolved[0].message_id == "msg_u1"


def test_out_of_range_ref_reports_code(tmp_path: pathlib.Path) -> None:
    session_store, _root, _blocks, refs = support.stores(tmp_path)
    session = support.seed_session(session_store)
    with pytest.raises(CompactError) as info:
        resolve_refs(session, ["m00099"], refs)
    assert info.value.code == "COMPACT_REF_NOT_FOUND"
    assert info.value.detail["available"] == 6


def test_duplicate_ref_in_one_call_is_rejected(tmp_path: pathlib.Path) -> None:
    session_store, _root, _blocks, refs = support.stores(tmp_path)
    session = support.seed_session(session_store)
    with pytest.raises(CompactError) as info:
        resolve_refs(session, ["m00001", "M1"], refs)
    assert info.value.code == "COMPACT_REF_CONFLICT"


def test_register_is_idempotent_for_same_seq_and_conflicts_otherwise(tmp_path: pathlib.Path) -> None:
    refs = RefStore(tmp_path)
    entry = RefEntry(ref="m00001", seq=4, message_id="msg_a", assigned_at=1)
    refs.register("session-test", [entry])
    assert refs.register("session-test", [RefEntry("m00001", 4, "msg_a", 2)]) == []
    with pytest.raises(CompactError) as info:
        refs.register("session-test", [RefEntry("m00001", 9, "msg_b", 3)])
    assert info.value.code == "COMPACT_REF_CONFLICT"
    assert refs.load("session-test")[0]["m00001"].seq == 4


def test_corrupt_ref_lines_are_skipped(tmp_path: pathlib.Path) -> None:
    refs = RefStore(tmp_path)
    refs.register("session-test", [RefEntry("m00001", 1, "msg", 1)])
    with refs.path("session-test").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("garbage\n")
        handle.write('{"ref":"m00002"}\n')
    entries, corrupt = refs.load("session-test")
    assert list(entries) == ["m00001"]
    assert corrupt == 2


def test_ref_offsets_and_rollback(tmp_path: pathlib.Path) -> None:
    refs = RefStore(tmp_path)
    refs.register("session-test", [RefEntry("m00001", 1, "msg", 1)])
    offset = refs.offsets("session-test")
    refs.register("session-test", [RefEntry("m00002", 2, "msg2", 2)])
    assert len(refs.entries("session-test")) == 2
    refs.rollback("session-test", offset)
    assert [entry.ref for entry in refs.entries("session-test")] == ["m00001"]


def test_ref_entry_round_trip_and_validation() -> None:
    entry = RefEntry(ref="m00003", seq=7, message_id="msg_x", assigned_at=9)
    assert RefEntry.from_dict(entry.to_dict()) == entry
    with pytest.raises(TypeError):
        RefEntry.from_dict(["bad"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RefEntry.from_dict({"ref": "m00001", "seq": -1})
    with pytest.raises(CompactError):
        RefEntry.from_dict({"ref": "nope", "seq": 1})
