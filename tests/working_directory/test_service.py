"""Tests for the per-session working-directory service."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.errors import AbortedError, ConfigError, ToolError
from deeppyro.working_directory import WorkingDirectoryService


class FakeSession:
    def __init__(self, session_id: str, cwd: Path | None = None) -> None:
        self.session_id = session_id
        self.header = None if cwd is None else SimpleNamespace(cwd=str(cwd))


def make_service(tmp_path: Path, default: Path | None = None) -> tuple[WorkingDirectoryService, Path]:
    base = default or (tmp_path / "work")
    if default is None:
        base.mkdir(parents=True, exist_ok=True)
    service = WorkingDirectoryService({"defaultDirectory": str(base)})
    return service, base


async def test_get_returns_default_without_filesystem_io(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    service, _ = make_service(tmp_path, missing)
    session = FakeSession("session-a")
    assert service.get(session) == missing.resolve(strict=False)
    assert service.get("session-a") == missing.resolve(strict=False)


async def test_get_uses_header_cwd_on_first_access(tmp_path: Path) -> None:
    header_cwd = tmp_path / "project"
    header_cwd.mkdir()
    service, _ = make_service(tmp_path)
    session = FakeSession("session-a", header_cwd)
    assert service.get(session) == header_cwd.resolve()


async def test_default_directory_must_be_absolute() -> None:
    with pytest.raises(ConfigError, match="defaultDirectory must be absolute"):
        WorkingDirectoryService({"defaultDirectory": "relative/dir"})


async def test_default_directory_attribute_config(tmp_path: Path) -> None:
    base = tmp_path / "configured"
    service = WorkingDirectoryService(SimpleNamespace(defaultDirectory=str(base)))
    assert service.default_directory == base.resolve(strict=False)


async def test_set_relative_path_commits_record_and_notice(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    target = base / "src"
    target.mkdir(parents=True)
    session = FakeSession("session-a")
    records: list[tuple[str, dict[str, Any]]] = []
    notices: list[str] = []
    service.configure(record=lambda kind, data: records.append((kind, data)), inject_notice=notices.append)

    result = await service.set(session, "src")

    assert result == target.resolve()
    assert service.get(session) == target.resolve()
    assert records == [("working-directory/change", {"cwd": str(target.resolve())})]
    assert notices == [
        "The working directory changed from "
        + json.dumps(str(base.resolve()), ensure_ascii=False)
        + " to "
        + json.dumps(str(target.resolve()), ensure_ascii=False)
        + "."
    ]


async def test_set_absolute_path_and_path_object(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    target = tmp_path / "other"
    target.mkdir()
    service.configure(record=lambda *_: None)
    assert await service.set("session-a", target) == target.resolve()
    assert await service.set("session-a", str(base)) == base.resolve()


async def test_set_same_path_does_not_record(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    records: list[tuple[str, dict[str, Any]]] = []
    service.configure(record=lambda kind, data: records.append((kind, data)))
    assert await service.set("session-a", base) == base.resolve()
    assert records == []


async def test_set_rejects_empty_and_non_string(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    service.configure(record=lambda *_: None)
    with pytest.raises(ToolError, match="cd must be a string"):
        await service.set("session-a", 3)  # type: ignore[arg-type]
    with pytest.raises(ToolError, match="cd must not be empty"):
        await service.set("session-a", "")
    with pytest.raises(ToolError, match="cd must not be empty"):
        await service.set("session-a", "   ")


async def test_set_rejects_missing_and_non_directory(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    service.configure(record=lambda *_: None)
    with pytest.raises(ToolError, match="directory does not exist"):
        await service.set("session-a", tmp_path / "missing")
    file_path = tmp_path / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(ToolError, match="directory does not exist"):
        await service.set("session-a", file_path)


async def test_set_without_record_callback_is_an_error(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ToolError, match="not configured"):
        await service.set("session-a", target)


async def test_set_record_failure_does_not_commit(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir()

    def failing_record(kind: str, data: dict[str, Any]) -> None:
        raise RuntimeError("append failed")

    service.configure(record=failing_record)
    with pytest.raises(RuntimeError, match="append failed"):
        await service.set("session-a", target)
    assert service.get("session-a") == base.resolve()


async def test_notice_failure_does_not_roll_back(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    notices: list[str] = []

    def failing_notice(text: str) -> None:
        notices.append(text)
        raise RuntimeError("inject failed")

    service.configure(record=lambda *_: None, inject_notice=failing_notice)
    assert await service.set("session-a", target) == target.resolve()
    assert service.get("session-a") == target.resolve()
    assert notices


async def test_cancel_before_set_and_ensure(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    service.configure(record=lambda *_: None)
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(AbortedError):
        await service.set("session-a", tmp_path, cancel=cancel)
    with pytest.raises(AbortedError):
        await service.ensure("session-a", cancel=cancel)


async def test_ensure_returns_existing_directory(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    assert await service.ensure("session-a") == base.resolve()


async def test_ensure_reports_vanished_directory(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    base.rmdir()
    with pytest.raises(ToolError, match="current directory is unavailable"):
        await service.ensure("session-a")


async def test_sessions_are_isolated(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    service.configure(record=lambda *_: None)
    await service.set("session-a", first)
    assert service.get("session-a") == first.resolve()
    assert service.get("session-b") == base.resolve()
    await service.set("session-b", second)
    assert service.get("session-a") == first.resolve()


async def test_bind_installs_per_session_callbacks_and_cwd(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    s1 = FakeSession("session-a")
    s2 = FakeSession("session-b")
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    d1.mkdir()
    d2.mkdir()
    records: list[tuple[str, str, dict[str, Any]]] = []
    notices: list[tuple[str, str]] = []
    assert (
        service.bind(
            s1,
            cwd=d1,
            record=lambda k, d: records.append(("a", k, d)),
            inject_notice=lambda t: notices.append(("a", t)),
        )
        == d1.resolve()
    )
    assert (
        service.bind(
            s2,
            cwd=d2,
            record=lambda k, d: records.append(("b", k, d)),
            inject_notice=lambda t: notices.append(("b", t)),
        )
        == d2.resolve()
    )
    target = tmp_path / "target"
    target.mkdir()
    await service.set(s1, target)
    assert service.get(s1) == target.resolve()
    assert [r[0] for r in records] == ["a"]
    assert [n[0] for n in notices] == ["a"]
    service.unbind(s1)
    assert service.get(s1) == service.default_directory


async def test_concurrent_sets_for_one_session_serialize(tmp_path: Path) -> None:
    service, _base = make_service(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    records: list[dict[str, Any]] = []
    service.configure(record=lambda kind, data: records.append(data))
    results = await asyncio.gather(service.set("session-a", first), service.set("session-a", second))
    assert set(results) == {first.resolve(), second.resolve()}
    assert len(records) == 2


async def test_context_text_uses_json_quoted_path(tmp_path: Path) -> None:
    service, base = make_service(tmp_path)
    assert (
        service.context_text("session-a")
        == "Current working directory: " + json.dumps(str(base.resolve()), ensure_ascii=False) + "."
    )


async def test_configure_replaces_default_directory(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    service.configure(default_directory=replacement)
    assert service.get("session-a") == replacement.resolve()


async def test_session_id_must_not_be_empty(tmp_path: Path) -> None:
    service, _ = make_service(tmp_path)
    with pytest.raises(ToolError, match="session id must not be empty"):
        service.get("")
    with pytest.raises(ToolError, match="session has no session_id"):
        service.get(object())
