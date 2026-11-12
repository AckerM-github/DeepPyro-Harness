"""Reader and zstd framing tests against the real session fixtures."""

from __future__ import annotations

import itertools
import json
import pathlib
from collections import Counter

import pytest

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import LogRecord
from deeppyro.session import events, layout, reader, writer
from tests.session import support

if not support.FIXTURES.is_dir():
    pytest.skip(
        "real DSH fixtures not present; run: node tools/gen_fixtures.mjs",
        allow_module_level=True,
    )

MANIFEST = support.manifest_by_file()
TYPE_SAMPLES = support.load_manifest()["typeSamples"]


@pytest.mark.parametrize("path", support.session_files(), ids=lambda p: p.stem)
def test_read_every_real_session(path: pathlib.Path) -> None:
    entry = MANIFEST[path.name]
    result = reader.read_session_file(path)
    assert result.header.id == path.stem == entry["id"]
    assert result.truncated is False
    assert entry["badLines"] == 0
    expected = entry["keptCount"] if entry["trimmed"] else entry["recordCount"]
    assert len(result.records) == expected
    assert result.trimmed is entry["trimmed"]
    seqs = [record.seq for record in result.records]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    if not result.trimmed:
        assert seqs == list(range(len(seqs)))
        assert Counter(record.type for record in result.records) == Counter(entry["types"])
    else:
        gaps = sum(1 for left, right in itertools.pairwise(seqs) if right != left + 1)
        assert gaps == 1


@pytest.mark.parametrize("path", support.by_type_files(), ids=lambda p: p.stem)
def test_every_record_type_sample_validates(path: pathlib.Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    sample = TYPE_SAMPLES[path.stem]
    assert raw == sample["sample"]
    normalized = events.normalize_event(raw)
    record = LogRecord.model_validate(normalized)
    assert record.type == sample["type"]
    session_file = support.SESSIONS_DIR / f"{sample['sessionId']}.jsonl"
    assert session_file.exists() or sample["sessionId"] in {entry["id"] for entry in MANIFEST.values()}


def test_type_samples_cover_every_fixture_record_type() -> None:
    observed = set()
    for path in support.session_files():
        observed.update(record.type for record in reader.read_session_file(path).records)
    sampled = {sample["type"] for sample in TYPE_SAMPLES.values()}
    assert observed <= sampled
    assert len(sampled) == 42


def test_torn_tail_is_dropped_and_flagged(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.v4.jsonl"
    header = support.header("session-torn")
    writer.write_new_session(path, header, [support.event("turn/start", 0, {"turn": 1})])
    with path.open("ab") as handle:
        handle.write(b'{"type":"step/start","seq":1')
    result = reader.read_session_file(path)
    assert result.truncated is True
    assert [record.seq for record in result.records] == [0]


def test_corrupt_header_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.v4.jsonl"
    path.write_text('{"type":"session","version":3}\n', encoding="utf-8")
    with pytest.raises(SessionError):
        reader.read_session_file(path)
    with pytest.raises(SessionError):
        reader.read_header(path)


def test_non_increasing_seq_is_rejected(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.v4.jsonl"
    header = support.header("session-bad-seq")
    writer.write_new_session(
        path,
        header,
        [
            support.event("turn/start", 0, {"turn": 1}),
            support.event("turn/end", 0, {"turn": 1}),
        ],
    )
    with pytest.raises(SessionError, match="not increasing"):
        reader.read_session_file(path)


def test_trimmed_gap_is_tolerated(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.v4.jsonl"
    header = support.header("session-gap")
    writer.write_new_session(
        path,
        header,
        [
            support.event("turn/start", 0, {"turn": 1}),
            support.event("turn/end", 7, {"turn": 1}),
        ],
    )
    result = reader.read_session_file(path)
    assert result.trimmed is True
    assert [record.seq for record in result.records] == [0, 7]


def test_zstd_roundtrip_and_append_frames(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "session.v4.jsonl.zstd"
    header = support.header("session-zstd")
    writer.write_new_session(
        path,
        header,
        [support.event("turn/start", 0, {"turn": 1})],
        zstd=True,
    )
    assert reader.read_header(path).id == "session-zstd"
    writer.append_events(path, [support.event("turn/end", 1, {"turn": 1})], zstd=True)
    result = reader.read_session_file(path)
    assert [record.type for record in result.records] == ["turn/start", "turn/end"]
    assert result.trimmed is False


def test_decompress_zstd_frames_stops_at_torn_frame() -> None:
    import zstandard

    compressor = zstandard.ZstdCompressor(write_checksum=True)
    first = compressor.compress(b'{"one":1}\n')
    second = compressor.compress(b'{"two":2}\n')
    assert reader.decompress_zstd_frames(first + second) == b'{"one":1}\n{"two":2}\n'
    torn = reader.decompress_zstd_frames(first + second[: len(second) // 2])
    assert torn.startswith(b'{"one":1}\n')
    assert b'"two"' not in torn
    assert reader.decompress_zstd_frames(b"") == b""


def test_discover_session_files_supports_flat_and_nested_layouts(tmp_path: pathlib.Path) -> None:
    flat = layout.flat_session_path(tmp_path, "session-flat")
    nested = layout.session_file_path(tmp_path, "session-nested", "C:/work")
    writer.write_new_session(flat, support.header("session-flat"), [])
    writer.write_new_session(nested, support.header("session-nested"), [])
    discovered = {path.resolve() for path in layout.discover_session_files(tmp_path)}
    assert flat.resolve() in discovered
    assert nested.resolve() in discovered


def test_zstd_roundtrip_of_a_large_real_log(tmp_path: pathlib.Path) -> None:
    source = support.SESSIONS_DIR / "session-18982fa0-64ab-4f8b-bf36-f70a6f67b44b.jsonl"
    original = reader.read_session_file(source)
    assert len(original.records) > 500
    target = tmp_path / "large.jsonl.zstd"
    writer.write_new_session(
        target,
        original.header,
        [writer.event_to_dict(record) for record in original.records],
        zstd=True,
    )
    assert target.stat().st_size > 200_000
    restored = reader.read_session_file(target)
    assert restored.header == original.header
    assert [record.model_dump() for record in restored.records] == [
        record.model_dump() for record in original.records
    ]
    assert restored.trimmed is False
    assert restored.truncated is False
