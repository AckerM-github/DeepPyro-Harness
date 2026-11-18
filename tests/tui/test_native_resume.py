"""Native session browsing, resume, fork, rename and archive in native TUI mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import deeppyro.session.native_index as native_index
from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.session.native_control import NativeSessionControl, NewSession
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.app_sessions import SessionLayer
from deeppyro.tui.format import short_session_id
from tests.session.native_helpers import write_native

SESSION_ONE = "session-00000000-0000-0000-0000-000000000101"
SESSION_TWO = "session-00000000-0000-0000-0000-000000000102"
CREATED_AT = 1_700_000_000_000


class _Conversation:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.users: list[str] = []
        self.assistant: list[str] = []
        self.finished: list[str] = []
        self.processes: list[Any] = []
        self.clears = 0

    def add_info(self, text: str) -> None:
        self.infos.append(str(text))

    def add_error(self, text: str) -> None:
        self.errors.append(str(text))

    def add_user(self, text: str) -> None:
        self.users.append(str(text))

    def stream_text(self, text: str) -> None:
        self.assistant.append(str(text))

    def finish_assistant(self, text: str) -> None:
        self.finished.append(str(text))

    def add_history_process(self, process: Any) -> None:
        self.processes.append(process)

    def clear(self) -> None:
        self.clears += 1


class _SessionsFacade:
    """The slice of SessionsPlugin the session browser uses."""

    def __init__(self, control: NativeSessionControl) -> None:
        self.control = control

    def native_fork(self, session_id: str, *, new_id: str | None = None) -> Any:
        return self.control.fork(session_id, new_id=new_id)

    def native_archive(self, session_id: str, *, archived: bool = True) -> bool:
        return self.control.archive(session_id, archived=archived)


class _Runtime:
    """A native runtime backed by a real session control plane on disk."""

    native = True

    def __init__(self, control: NativeSessionControl, cwd: Path) -> None:
        self.control = control
        self.sessions = _SessionsFacade(control)
        self.cwd = cwd
        self.session: NativeSessionHandle | None = None
        self.agent: Any = None
        self.opened: list[str] = []

    @property
    def session_id(self) -> str:
        return self.session.session_id if self.session is not None else ""

    def recent_sessions(self, limit: int = 20, *, include_archived: bool = False) -> list[Any]:
        return self.control.list(include_archived=include_archived, limit=limit)

    def archived_sessions(self, limit: int = 20) -> list[Any]:
        archived = [info for info in self.control.list(include_archived=True) if info.archived]
        return archived[:limit]

    def _release(self) -> None:
        handle = self.session
        self.session = None
        self.agent = None
        if handle is not None:
            handle.aclose()

    async def open_session(self, session_id: str) -> NativeSessionHandle:
        self.opened.append(session_id)
        self._release()
        handle = NativeSessionHandle(self.control.open(session_id))
        self.session = handle
        return handle

    async def new_session(self) -> NativeSessionHandle:
        self._release()
        session = self.control.create(NewSession(cwd=self.cwd))
        handle = NativeSessionHandle(session)
        self.session = handle
        return handle

    async def _detach(self) -> None:
        self._release()


class _Host(SessionLayer):
    def __init__(self, runtime: _Runtime, *, pick: str | None = None) -> None:
        self.runtime = runtime
        self._conversation = _Conversation()
        self._working = False
        self._context_used = 0
        self.refreshes = 0
        self.notifications: list[tuple[str, str | None]] = []
        self.renders: list[str] = []
        self.pick_call: tuple[str, list[tuple[str, str, str]]] | None = None
        self._pick_result = pick

    @property
    def conversation(self) -> _Conversation:
        return self._conversation

    async def _pick(self, title: str, items: list[tuple[str, str, str]]) -> str | None:
        self.pick_call = (title, items)
        return self._pick_result

    def notify(self, message: str, severity: str | None = None) -> None:
        self.notifications.append((message, severity))

    def _render_welcome(self) -> None:
        self.renders.append("welcome")

    def _render_history(self) -> None:
        self.renders.append("history")

    def refresh_status(self) -> None:
        self.refreshes += 1


class _HistoryHost(_Host):
    """Keeps the real history renderer so attached sessions are really drawn."""

    _render_history = SessionLayer._render_history


def _control(tmp_path: Path) -> NativeSessionControl:
    return NativeSessionControl(root=tmp_path / "sessions")


def _write(control: NativeSessionControl, session_id: str, *, created_at: int, **kwargs: Any) -> Path:
    return write_native(control.root, session_id=session_id, created_at=created_at, **kwargs)


def _host(tmp_path: Path) -> _Host:
    control = _control(tmp_path)
    return _Host(_Runtime(control, tmp_path))


async def test_sessions_lists_native_sessions_with_attach_hint(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="first session")
    _write(control, SESSION_TWO, created_at=CREATED_AT + 10_000, title="second session")
    host = _Host(_Runtime(control, tmp_path))

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    lines = host.conversation.infos
    assert lines[0] == "sessions (2):"
    joined = "\n".join(lines)
    assert "first session" in joined
    assert "second session" in joined
    assert "[native]" not in joined
    assert "  native" in joined
    assert lines[-1] == DeepPyroApp.NATIVE_ATTACH_HINT
    assert "read-only" not in joined
    assert host.conversation.errors == []


async def test_resume_attaches_a_native_session_by_index(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="older session")
    _write(control, SESSION_TWO, created_at=CREATED_AT + 10_000, title="newer session")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)

    await DeepPyroApp.resume(host, "1")  # type: ignore[arg-type]

    assert runtime.session_id == SESSION_TWO
    assert runtime.opened == [SESSION_TWO]
    assert host.conversation.clears == 1
    assert host.renders == ["welcome", "history"]
    assert f"resumed {short_session_id(SESSION_TWO)}" in host.conversation.infos
    assert host.conversation.errors == []


async def test_resume_attaches_a_native_session_by_id(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="only session")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)

    await DeepPyroApp.resume(host, SESSION_ONE)  # type: ignore[arg-type]

    assert runtime.session_id == SESSION_ONE
    assert f"resumed {short_session_id(SESSION_ONE)}" in host.conversation.infos


async def test_resume_attaches_a_session_matched_by_title(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="parser repair")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)

    await DeepPyroApp.resume(host, "parser")  # type: ignore[arg-type]

    assert runtime.session_id == SESSION_ONE


async def test_resume_without_argument_attaches_the_picked_session(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="picked session")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime, pick=SESSION_ONE)

    await DeepPyroApp.resume(host, "")  # type: ignore[arg-type]

    assert host.pick_call is not None
    assert runtime.session_id == SESSION_ONE


async def test_resume_locked_session_shows_a_read_only_transcript(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="busy session")
    held = control.open(SESSION_ONE)
    try:
        runtime = _Runtime(control, tmp_path)
        host = _Host(runtime)

        await DeepPyroApp.resume(host, SESSION_ONE)  # type: ignore[arg-type]

        assert runtime.session is None
        assert host.conversation.users == ["Please read the file."]
        assert "I read the file." in host.conversation.assistant
        joined = "\n".join(host.conversation.infos)
        assert "another process owns this session" in joined
        assert "read-only transcript" in joined
        assert host.conversation.clears == 0
    finally:
        held.close()


async def test_resume_damaged_log_shows_the_fold_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="broken fold")
    original = native_index.fold

    def broken(header: Any, events: Any) -> Any:
        if getattr(header, "id", "") == SESSION_ONE:
            raise ValueError("cannot fold this log")
        return original(header, events)

    monkeypatch.setattr(native_index, "fold", broken)
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)

    await DeepPyroApp.resume(host, SESSION_ONE)  # type: ignore[arg-type]

    assert runtime.session is None
    joined = "\n".join(host.conversation.infos)
    assert "did not fold cleanly" in joined
    assert "fold note:" in joined


async def test_corrupt_logs_are_skipped_without_breaking_the_browser(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="healthy session")
    corrupt = control.root / "session-00000000-0000-0000-0000-000000000999.jsonl"
    corrupt.write_text("{not json at all", encoding="utf-8")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]
    assert host.conversation.infos[0] == "sessions (1):"
    assert not any("999" in line for line in host.conversation.infos)

    await DeepPyroApp.resume(host, "session-00000000-0000-0000-0000-000000000999")  # type: ignore[arg-type]

    assert any("session not found" in error for error in host.conversation.errors)


async def test_resume_really_renders_the_attached_history(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(
        control,
        SESSION_ONE,
        created_at=CREATED_AT,
        title="history session",
        user_text="hello from the log",
        assistant_text="hello from the assistant",
    )
    runtime = _Runtime(control, tmp_path)
    host = _HistoryHost(runtime)

    await DeepPyroApp.resume(host, SESSION_ONE)  # type: ignore[arg-type]

    assert "hello from the log" in host.conversation.users
    assert "hello from the assistant" in host.conversation.assistant
    assert "hello from the assistant" in host.conversation.finished
    assert host.conversation.processes
    assert host.conversation.processes[0].tools[0].name == "read_file"


async def test_fork_copies_the_current_session(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="fork source")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)
    await runtime.open_session(SESSION_ONE)

    await DeepPyroApp.fork_command(host, "")  # type: ignore[arg-type]

    new_id = runtime.session_id
    assert new_id
    assert new_id != SESSION_ONE
    assert control.look(new_id).parent_id == SESSION_ONE
    joined = "\n".join(host.conversation.infos)
    assert f"forked {short_session_id(SESSION_ONE)} -> {short_session_id(new_id)}" in joined
    assert host.conversation.clears == 1
    reopened = control.open(SESSION_ONE)
    reopened.close()


async def test_fork_starts_from_the_current_session_when_named(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="first")
    _write(control, SESSION_TWO, created_at=CREATED_AT + 10_000, title="second")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)
    await runtime.open_session(SESSION_TWO)

    await DeepPyroApp.fork_command(host, "1")  # type: ignore[arg-type]

    assert control.look(runtime.session_id).parent_id == SESSION_TWO


async def test_rename_records_a_native_title(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="old title")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)
    await runtime.open_session(SESSION_ONE)

    DeepPyroApp.rename_command(host, "renamed by the user")  # type: ignore[arg-type]

    assert runtime.session is not None
    assert runtime.session.fold().state.title == "renamed by the user"
    assert any("renamed this session" in info for info in host.conversation.infos)


async def test_archive_then_unarchive_round_trip(tmp_path: Path) -> None:
    control = _control(tmp_path)
    _write(control, SESSION_ONE, created_at=CREATED_AT, title="archivable")
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)
    await runtime.open_session(SESSION_ONE)

    await DeepPyroApp.archive_command(host)  # type: ignore[arg-type]

    assert control.look(SESSION_ONE).archived is True
    assert runtime.session_id != SESSION_ONE
    DeepPyroApp.sessions_command(host, "--all")  # type: ignore[arg-type]
    archived_lines = host.conversation.infos
    assert any(line.startswith("archived sessions") for line in archived_lines)
    assert any("archived" in line and short_session_id(SESSION_ONE) in line for line in archived_lines)

    DeepPyroApp.unarchive_command(host, "1")  # type: ignore[arg-type]

    assert control.look(SESSION_ONE).archived is False
    assert any("restored" in info for info in host.conversation.infos)


async def test_fork_reports_missing_sources(tmp_path: Path) -> None:
    control = _control(tmp_path)
    runtime = _Runtime(control, tmp_path)
    host = _Host(runtime)

    await DeepPyroApp.fork_command(host, "session-00000000-0000-0000-0000-000000000777")  # type: ignore[arg-type]

    assert any("fork failed" in error for error in host.conversation.errors)
