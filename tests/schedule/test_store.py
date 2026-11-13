"""ScheduleStore: version checks, atomic writes and corruption quarantine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deeppyro.schedule import ScheduleStore, ScheduleStoreError, default_schedules_path
from deeppyro.schedule.types import AfterTiming, ScheduleRecord, SessionTarget, record_to_dict


def _record(task_id: str = "schedule-1", title: str = "standup") -> ScheduleRecord:
    stamp = "2026-11-14T02:00:00.000Z"
    return ScheduleRecord(
        id=task_id,
        title=title,
        prompt="say hi",
        kind="after",
        timing=AfterTiming(after_seconds=60),
        scheduled_at="2026-11-14T02:01:00.000Z",
        status="active",
        target=SessionTarget(session_id="session-1"),
        created_at=stamp,
        updated_at=stamp,
    )


def test_missing_file_loads_empty(tmp_path: Path) -> None:
    assert ScheduleStore(tmp_path / "schedules.json").load() == {}


def test_roundtrip_preserves_record(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "schedules.json"
    store = ScheduleStore(path)
    record = _record()
    store.save({record.id: record})
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert list(data["tasks"]) == ["schedule-1"]
    assert ScheduleStore(path).load() == {"schedule-1": record}


def test_save_leaves_no_temporary_files(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path / "schedules.json")
    store.save({"schedule-1": _record()})
    store.save({"schedule-2": _record("schedule-2")})
    assert sorted(path.name for path in tmp_path.iterdir()) == ["schedules.json"]


def test_default_path_lives_under_deeppyro_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path))
    assert default_schedules_path() == tmp_path / "schedules.json"


def test_corrupt_json_raises_and_quarantines(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text("{not json", encoding="utf-8")
    store = ScheduleStore(path)
    with pytest.raises(ScheduleStoreError, match="not valid JSON"):
        store.load()
    quarantined = store.quarantine()
    assert quarantined is not None and quarantined.exists()
    assert ".corrupt-" in quarantined.name
    assert not path.exists()
    assert store.quarantine() is None


def test_unsupported_version_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps({"version": 99, "tasks": {}}), encoding="utf-8")
    with pytest.raises(ScheduleStoreError, match="unsupported version"):
        ScheduleStore(path).load()


def test_invalid_record_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(json.dumps({"version": 1, "tasks": {"x": {"id": "x"}}}), encoding="utf-8")
    with pytest.raises(ScheduleStoreError, match="is invalid"):
        ScheduleStore(path).load()


def test_store_keys_by_record_id(tmp_path: Path) -> None:
    record = _record("custom-id")
    path = tmp_path / "schedules.json"
    payload = {"version": 1, "tasks": {"other": record_to_dict(record)}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert list(ScheduleStore(path).load()) == ["custom-id"]
