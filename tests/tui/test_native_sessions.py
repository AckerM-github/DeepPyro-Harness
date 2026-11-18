"""Native schema 1 sessions in the TUI session browser: listing and resume."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from deeppyro.core.messages import Message, ModelSource, TextPart, UserSource
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.native_index import NativeSessionInfo, list_native_sessions
from deeppyro.session.schema import EventKind, NativeSession
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.app_sessions import SessionLayer
from deeppyro.tui.format import short_session_id
from deeppyro.tui.runtime_status import StatusLayer

NATIVE_ID = "session-11112222-3333-4444-5555-666677778888"
OTHER_ID = "session-22223333-4444-5555-6666-777788889999"
LIVE_ID = "session-aaaabbbb-cccc-dddd-eeee-ffff00001111"
RUN_ID = "run_" + "1" * 16
TURN_ID = "turn_" + "2" * 16
STEP_ID = "step_" + "3" * 16
TRACE_ID = "b" * 32
CREATED_AT = 1_700_000_000_000


def _native_session(
    directory: Path,
    *,
    session_id: str = NATIVE_ID,
    title: str = "Native parser fix",
    message: str = "please summarize the logs",
    created_at: int = CREATED_AT,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    with NativeSession.create(path, cwd="C:/work/native", created_at=created_at) as session:
        session.append(
            EventKind.TURN_STARTED,
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
            ts=created_at + 1,
        )
        user = Message(
            id="msg_" + "5" * 16,
            role="user",
            content=(TextPart(message),),
            created_at=created_at + 1,
            source=UserSource(),
        )
        session.append(
            EventKind.USER_MESSAGE,
            {"message": user.to_dict()},
            turn_id=TURN_ID,
            ts=created_at + 2,
        )
        session.append(
            EventKind.STEP_STARTED,
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
            ts=created_at + 3,
        )
        assistant = Message(
            id="msg_" + "6" * 16,
            role="assistant",
            content=(TextPart("the logs look healthy"),),
            created_at=created_at + 3,
            source=ModelSource(provider="fake", model="fake-1"),
        )
        session.append(
            EventKind.ASSISTANT_MESSAGE,
            {"message": assistant.to_dict(), "finish_reason": "stop"},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
            ts=created_at + 4,
        )
        session.append(
            EventKind.STEP_ENDED,
            {},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            step_id=STEP_ID,
            trace_id=TRACE_ID,
            ts=created_at + 5,
        )
        session.append(EventKind.SESSION_TITLE, {"title": title, "source": "user"}, ts=created_at + 6)
        session.append(
            EventKind.TURN_ENDED,
            {"reason": {"kind": "completed"}},
            run_id=RUN_ID,
            turn_id=TURN_ID,
            trace_id=TRACE_ID,
            ts=created_at + 7,
        )
    return path


class _Conversation:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.users: list[str] = []
        self.assistant: list[str] = []
        self.finished: list[str] = []
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

    def clear(self) -> None:
        self.clears += 1


class _Runtime:
    """Native-only runtime surface used by the session layer."""

    def __init__(self, sessions_dir: Path) -> None:
        self.sessions_dir = sessions_dir
        self.opened_sessions: list[str] = []
        self.session = SimpleNamespace(session_id=LIVE_ID)

    def _infos(self, *, include_archived: bool = False) -> list[NativeSessionInfo]:
        return list_native_sessions(self.sessions_dir, include_archived=include_archived)

    def recent_sessions(self, limit: int = 20, *, include_archived: bool = False) -> list[NativeSessionInfo]:
        return self._infos(include_archived=include_archived)[:limit]

    def archived_sessions(self, limit: int = 20) -> list[NativeSessionInfo]:
        return self._infos(include_archived=True)[:limit]

    async def open_session(self, session_id: str) -> object:
        self.opened_sessions.append(session_id)
        self.session = SimpleNamespace(session_id=session_id)
        return self.session


class _LockedRuntime(_Runtime):
    async def open_session(self, session_id: str) -> object:
        raise SessionLockedError(session_id)


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


def _host(tmp_path: Path, *, runtime_cls: type[_Runtime] = _Runtime, pick: str | None = None) -> _Host:
    sessions_dir = tmp_path / "sessions"
    _native_session(sessions_dir)
    return _Host(runtime_cls(sessions_dir), pick=pick)


async def test_sessions_lists_native_rows_with_a_label(tmp_path: Path) -> None:
    host = _host(tmp_path)

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    lines = host.conversation.infos
    assert lines[0] == "sessions (1):"
    row = next(line for line in lines if "Native parser fix" in line)
    assert "native" in row
    assert lines[-1] == DeepPyroApp.NATIVE_ATTACH_HINT
    assert host.conversation.errors == []


async def test_sessions_orders_rows_by_recency(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _native_session(sessions_dir, session_id=NATIVE_ID, title="older session", created_at=CREATED_AT)
    _native_session(sessions_dir, session_id=OTHER_ID, title="newer session", created_at=CREATED_AT + 100_000)
    host = _Host(_Runtime(sessions_dir))

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    lines = host.conversation.infos
    assert lines[0] == "sessions (2):"
    newer = next(i for i, line in enumerate(lines) if "newer session" in line)
    older = next(i for i, line in enumerate(lines) if "older session" in line)
    assert newer < older


async def test_sessions_filters_rows_by_text(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _native_session(sessions_dir, session_id=NATIVE_ID, title="native parser fix")
    _native_session(sessions_dir, session_id=OTHER_ID, title="unrelated work", created_at=CREATED_AT + 10)
    host = _Host(_Runtime(sessions_dir))

    DeepPyroApp.sessions_command(host, "parser")  # type: ignore[arg-type]

    lines = host.conversation.infos
    assert lines[0].startswith("sessions matching 'parser'")
    assert any("native parser fix" in line for line in lines)
    assert not any("unrelated work" in line for line in lines)


async def test_resume_attaches_a_native_session(tmp_path: Path) -> None:
    host = _host(tmp_path)

    await DeepPyroApp.resume(host, NATIVE_ID)  # type: ignore[arg-type]

    assert host.runtime.opened_sessions == [NATIVE_ID]
    assert host.conversation.clears == 1
    assert host.renders == ["welcome", "history"]
    assert host.conversation.infos == [f"resumed {short_session_id(NATIVE_ID)}"]
    assert host.conversation.errors == []


async def test_resume_reads_the_transcript_when_the_session_is_locked(tmp_path: Path) -> None:
    host = _host(tmp_path, runtime_cls=_LockedRuntime)
    live = host.runtime.session

    await DeepPyroApp.resume(host, NATIVE_ID)  # type: ignore[arg-type]

    assert host.conversation.users == ["please summarize the logs"]
    assert host.conversation.assistant == ["the logs look healthy"]
    assert host.conversation.finished == ["the logs look healthy"]
    joined = "\n".join(host.conversation.infos)
    assert "native session" in joined
    assert "another process owns this session" in joined
    assert "still active" in joined
    assert host.runtime.opened_sessions == []
    assert host.runtime.session is live
    assert host.conversation.clears == 0
    assert host.renders == []
    assert host.refreshes == 1
    assert host.conversation.errors == []


async def test_resume_by_index(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _native_session(sessions_dir, session_id=NATIVE_ID, title="first")
    _native_session(sessions_dir, session_id=OTHER_ID, title="second", created_at=CREATED_AT + 10)
    host = _Host(_Runtime(sessions_dir))

    await DeepPyroApp.resume(host, "1")  # type: ignore[arg-type]
    assert host.runtime.opened_sessions == [OTHER_ID]

    await DeepPyroApp.resume(host, "2")  # type: ignore[arg-type]
    assert host.runtime.opened_sessions == [OTHER_ID, NATIVE_ID]


async def test_resume_matched_by_title_text(tmp_path: Path) -> None:
    host = _host(tmp_path)

    await DeepPyroApp.resume(host, "parser fix")  # type: ignore[arg-type]

    assert host.runtime.opened_sessions == [NATIVE_ID]


async def test_resume_picker_labels_native_rows(tmp_path: Path) -> None:
    host = _host(tmp_path, pick=NATIVE_ID)

    await DeepPyroApp.resume(host, "")  # type: ignore[arg-type]

    assert host.pick_call is not None
    title, items = host.pick_call
    assert title == "Resume session"
    labels = [label for _value, label, _note in items]
    assert any("native" in label for label in labels)
    assert host.runtime.opened_sessions == [NATIVE_ID]


async def test_unknown_session_is_an_error(tmp_path: Path) -> None:
    host = _host(tmp_path)

    await DeepPyroApp.resume(host, "session-does-not-exist")  # type: ignore[arg-type]

    assert host.conversation.errors == ["session not found: session-does-not-exist"]
    assert host.runtime.opened_sessions == []


async def test_malformed_native_files_do_not_break_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _native_session(sessions_dir)
    (sessions_dir / "junk.jsonl").write_text("not json\n", encoding="utf-8")
    host = _Host(_Runtime(sessions_dir))

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    assert host.conversation.errors == []
    assert host.conversation.infos[0] == "sessions (1):"
    assert host.conversation.infos[-1] == DeepPyroApp.NATIVE_ATTACH_HINT


def test_status_layer_lists_native_sessions_from_the_home(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _native_session(sessions_dir)
    status = object.__new__(StatusLayer)
    status.sessions = SimpleNamespace(  # type: ignore[attr-defined]
        native_list=lambda *, include_archived=False, limit=None: list_native_sessions(
            sessions_dir, include_archived=include_archived
        )[:limit]
    )

    infos = StatusLayer.native_sessions(status, 10)

    assert [info.id for info in infos] == [NATIVE_ID]
    assert StatusLayer.native_sessions(status, 0) == []
