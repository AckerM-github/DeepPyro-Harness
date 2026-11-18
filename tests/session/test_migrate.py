"""Session format migration: pure conversions, batch runs, CLI and service."""

from __future__ import annotations

import json
import pathlib
import shutil
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.config import Config
from deeppyro.migrate import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_PARTIAL,
    MigrationStatus,
    SessionFormat,
    detect_format,
    map_session_id,
    native_to_v4,
    run_migration,
    v4_to_native,
)
from deeppyro.session import migrate_cli
from deeppyro.session.derive import derive_messages
from deeppyro.session.fold import fold
from deeppyro.session.layout import discover_session_files
from deeppyro.session.reader import read_session_file
from deeppyro.session.schema import SESSION_ID_RE, NativeCodec
from deeppyro.tui.main import main as tui_main
from tests.service_doubles import service_lookup, service_require

SYNTHETIC = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "sessions"
SESSION_A = SYNTHETIC / "session-11111111-1111-4111-8111-111111111111.jsonl"
SESSION_B = SYNTHETIC / "session-22222222-2222-4222-8222-222222222222.jsonl"

NATIVE_TEXTS_A = [
    ("user", "hello again"),
    ("assistant", "first answer"),
    ("assistant", "calling read"),
    ("tool", "ok"),
]
ROUND_TRIP_TEXTS_A = [
    ("system", "system prompt"),
    ("user", "hello again"),
    ("assistant", "first answer"),
    ("assistant", "calling read"),
    ("tool", "ok"),
]


def _v4_root(tmp_path: pathlib.Path, *names: str) -> pathlib.Path:
    root = tmp_path / "v4"
    root.mkdir()
    for name in names or (SESSION_A.name,):
        shutil.copy2(SYNTHETIC / name, root / name)
    return root


def _messages_texts(messages: list[Any]) -> list[tuple[str, str]]:
    return [(message.role, message.text()) for message in messages]


def _run(to: SessionFormat | str, *, native: pathlib.Path, v4: pathlib.Path, **kwargs: Any) -> Any:
    return run_migration(to=to, all=True, native_root=native, v4_root=v4, **kwargs)


def test_v4_to_native_and_back_preserves_the_conversation(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path)
    native = tmp_path / "native"
    first = _run(SessionFormat.NATIVE, native=native, v4=v4)
    assert first.exit_code == EXIT_OK
    assert first.converted == 1
    entry = first.entries[0]
    assert entry.status is MigrationStatus.CONVERTED
    assert entry.session_id == SESSION_A.stem
    log = NativeCodec.read(native / f"{SESSION_A.stem}.jsonl")
    assert log.header.cwd == "C:\\work"
    assert log.header.preset == "standard"
    assert log.header.created_at == 1_700_000_000_000
    assert _messages_texts(list(fold(log.header, log.events).messages)) == NATIVE_TEXTS_A

    back = tmp_path / "back"
    second = _run(SessionFormat.V4, native=native, v4=back)
    assert second.exit_code == EXIT_OK
    assert second.converted == 1
    paths = list(discover_session_files(back))
    assert len(paths) == 1
    read = read_session_file(paths[0])
    assert read.header.id == SESSION_A.stem
    assert read.header.createdAt == 1_700_000_000_000
    messages = derive_messages(read.records)
    assert _messages_texts(messages) == ROUND_TRIP_TEXTS_A
    tool_calls = [part for message in messages for part in message.tool_calls()]
    assert [(part.name, part.arguments) for part in tool_calls] == [("read", "{}")]

    payload = paths[0].read_bytes()
    again = _run(SessionFormat.V4, native=native, v4=back)
    assert again.converted == 0
    assert again.exit_code == EXIT_OK
    assert paths[0].read_bytes() == payload


def test_repeated_conversion_is_idempotent(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path)
    native = tmp_path / "native"
    _run(SessionFormat.NATIVE, native=native, v4=v4)
    target = native / f"{SESSION_A.stem}.jsonl"
    payload = target.read_bytes()
    stamp = target.stat().st_mtime_ns
    report = _run(SessionFormat.NATIVE, native=native, v4=v4)
    assert report.converted == 0
    assert report.up_to_date == 2
    assert report.exit_code == EXIT_OK
    assert target.read_bytes() == payload
    assert target.stat().st_mtime_ns == stamp


def test_dry_run_plans_without_writing(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path)
    native = tmp_path / "native"
    report = _run(SessionFormat.NATIVE, native=native, v4=v4, dry_run=True)
    assert report.exit_code == EXIT_OK
    assert report.planned == 1
    assert report.converted == 0
    assert report.entries[0].status is MigrationStatus.PLANNED
    assert not native.exists()
    assert v4.joinpath(SESSION_A.name).read_bytes() == SESSION_A.read_bytes()


def test_corrupt_and_unknown_files_are_skipped(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path)
    (v4 / "broken.jsonl").write_bytes(b"not json at all\n")
    (v4 / "mystery.jsonl").write_bytes(json.dumps({"foo": 1}).encode() + b"\n")
    native = tmp_path / "native"
    report = _run(SessionFormat.NATIVE, native=native, v4=v4)
    assert report.converted == 1
    assert report.skipped == 2
    assert report.exit_code == EXIT_PARTIAL
    skipped = {
        entry.source.name: entry.reason for entry in report.entries if entry.status is MigrationStatus.SKIPPED
    }
    assert set(skipped) == {"broken.jsonl", "mystery.jsonl"}
    assert all(skipped.values())


def test_unconvertible_only_run_fails(tmp_path: pathlib.Path) -> None:
    v4 = tmp_path / "v4"
    v4.mkdir()
    (v4 / "broken.jsonl").write_bytes(b"{}\n")
    report = _run(SessionFormat.NATIVE, native=tmp_path / "native", v4=v4)
    assert report.skipped == 1
    assert report.exit_code == EXIT_FAILURE


def test_missing_session_fails_with_a_reason(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path)
    report = run_migration(
        to=SessionFormat.NATIVE,
        session="session-00000000-0000-4000-8000-000000000000",
        native_root=tmp_path / "native",
        v4_root=v4,
    )
    assert report.exit_code == EXIT_FAILURE
    assert report.failed == 1
    assert "not found" in (report.entries[-1].reason or "")


def test_session_selector_migrates_one_session(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path, SESSION_A.name, SESSION_B.name)
    native = tmp_path / "native"
    report = run_migration(to=SessionFormat.NATIVE, session=SESSION_B.stem, native_root=native, v4_root=v4)
    assert report.converted == 1
    assert report.entries[0].session_id == SESSION_B.stem
    assert (native / f"{SESSION_B.stem}.jsonl").is_file()
    assert not (native / f"{SESSION_A.stem}.jsonl").exists()


def test_existing_target_requires_yes_and_is_backed_up(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    target = native / f"{SESSION_A.stem}.jsonl"
    previous = b"previous native session bytes\n"
    target.write_bytes(previous)
    blocked = _run(SessionFormat.NATIVE, native=native, v4=v4)
    conflict = next(entry for entry in blocked.entries if entry.source == v4 / SESSION_A.name)
    assert conflict.status is MigrationStatus.SKIPPED
    assert "pass --yes" in (conflict.reason or "")
    assert blocked.exit_code == EXIT_FAILURE
    assert target.read_bytes() == previous
    assert not list(native.glob("*.bak*"))

    report = _run(SessionFormat.NATIVE, native=native, v4=v4, yes=True)
    assert report.converted == 1
    entry = next(entry for entry in report.entries if entry.status is MigrationStatus.CONVERTED)
    assert entry.backup is not None
    assert entry.backup.read_bytes() == previous
    NativeCodec.read(target)


def test_backup_can_be_disabled(tmp_path: pathlib.Path) -> None:
    v4 = _v4_root(tmp_path)
    native = tmp_path / "native"
    native.mkdir()
    target = native / f"{SESSION_A.stem}.jsonl"
    target.write_bytes(b"old bytes\n")
    report = _run(SessionFormat.NATIVE, native=native, v4=v4, yes=True, backup=False)
    assert report.converted == 1
    assert report.entries[0].backup is None
    assert not list(native.glob("*.bak*"))
    NativeCodec.read(target)


def test_detect_format_and_pure_conversions(tmp_path: pathlib.Path) -> None:
    assert detect_format(SESSION_A) is SessionFormat.V4
    read = read_session_file(SESSION_A)
    document = v4_to_native(read)
    native_path = tmp_path / f"{document.header.id}.jsonl"
    native_path.write_bytes(document.encode())
    assert detect_format(native_path) is SessionFormat.NATIVE
    log = NativeCodec.read(native_path)
    assert _messages_texts(list(fold(log.header, log.events).messages)) == NATIVE_TEXTS_A
    back = native_to_v4(log)
    restored = tmp_path / "restored.jsonl"
    restored.write_bytes(back.encode())
    assert _messages_texts(derive_messages(read_session_file(restored).records)) == ROUND_TRIP_TEXTS_A
    broken = tmp_path / "broken.jsonl"
    broken.write_bytes(b"{}\n")
    assert detect_format(broken) is SessionFormat.UNKNOWN


def test_map_session_id_is_stable_and_schema_valid() -> None:
    canonical = "session-11111111-1111-4111-8111-111111111111"
    assert map_session_id(canonical) == canonical
    legacy = "mnemon-task-v1-d2932f8c-bb85-4fa3-983b-1544b54332f7"
    mapped = map_session_id(legacy)
    assert SESSION_ID_RE.match(mapped)
    assert mapped == map_session_id(legacy)


def test_cli_reports_success_and_up_to_date(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v4 = _v4_root(tmp_path)
    native = tmp_path / "native"
    argv = ["--to", "native", "--all", "--v4-dir", str(v4), "--native-dir", str(native)]
    assert migrate_cli.main(argv) == EXIT_OK
    assert "converted=1" in capsys.readouterr().out
    assert migrate_cli.main(argv) == EXIT_OK
    assert "up-to-date=2" in capsys.readouterr().out


def test_cli_exit_codes_and_json_output(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    v4 = _v4_root(tmp_path)
    native = tmp_path / "native"
    assert (
        migrate_cli.main(["--to", "native", "--all", "--v4-dir", str(v4), "--native-dir", str(native)])
        == EXIT_OK
    )
    capsys.readouterr()
    (v4 / "broken.jsonl").write_bytes(b"{}\n")
    assert (
        migrate_cli.main(["--to", "native", "--all", "--v4-dir", str(v4), "--native-dir", str(native)])
        == EXIT_PARTIAL
    )
    capsys.readouterr()
    report = json.loads(
        _capture_json(
            capsys, ["--to", "native", "--all", "--json", "--v4-dir", str(v4), "--native-dir", str(native)]
        )
    )
    assert report["exitCode"] == EXIT_PARTIAL
    assert {entry["status"] for entry in report["entries"]} == {"up-to-date", "skipped"}


def _capture_json(capsys: pytest.CaptureFixture[str], argv: list[str]) -> str:
    code = migrate_cli.main(argv)
    assert code == EXIT_PARTIAL
    return capsys.readouterr().out


def test_cli_requires_a_selector() -> None:
    with pytest.raises(SystemExit):
        migrate_cli.main(["--to", "native"])


class _Reflect:
    def __init__(self) -> None:
        self.provided: dict[str, Any] = {}

    def provide(self, name: str, service: Any, check: Any = None) -> None:
        self.provided[name] = service


class _Ctx:
    def __init__(self, services: dict[str, Any]) -> None:
        self.reflect = _Reflect()
        self._services = services

    def service(self, name: str) -> Any:
        return self._services[name]

    def get(self, service: str | type[Any]) -> Any | None:
        return service_lookup(self._services, service)

    def require(self, service: type[Any]) -> Any:
        return service_require(self._services, service)


def test_session_migrate_service_runs_through_a_plugin_context(tmp_path: pathlib.Path) -> None:
    from deeppyro.session.migrate_service import SessionMigratePlugin

    v4 = _v4_root(tmp_path)
    cfg = Config.model_validate({"sessionDir": str(v4)})
    plugin = SessionMigratePlugin(_Ctx({"config": SimpleNamespace(value=cfg)}))
    assert SessionMigratePlugin.provide == "session_migrate"
    assert SessionMigratePlugin.inject == ["config"]
    assert plugin.name == "session_migrate"
    plugin.native_root = tmp_path / "native"
    report = plugin.migrate(to=SessionFormat.NATIVE, all=True)
    assert report.exit_code == EXIT_OK
    assert report.converted == 1
    assert (tmp_path / "native" / f"{SESSION_A.stem}.jsonl").is_file()


def test_tui_dispatches_the_migrate_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        captured.append(list(argv))
        return EXIT_PARTIAL

    monkeypatch.setattr("deeppyro.session.migrate_cli.main", fake_main)
    assert tui_main(["migrate", "--to", "native", "--all"]) == EXIT_PARTIAL
    assert captured == [["--to", "native", "--all"]]
