"""Native schema 1 sessions in the TUI session browser are visible but read-only."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from deeppyro.core.messages import Message, ModelSource, TextPart, UserSource
from deeppyro.session.native_index import list_native_sessions
from deeppyro.session.schema import EventKind, NativeSession
from deeppyro.tui.app import DeepPyroApp
from deeppyro.tui.app_sessions import SessionLayer
from deeppyro.tui.format import short_session_id
from deeppyro.tui.runtime_status import StatusLayer

NATIVE_ID = "session-11112222-3333-4444-5555-666677778888"
V4_ID = "session-99999999-8888-7777-6666-555555555555"
LIVE_ID = "session-aaaabbbb-cccc-dddd-eeee-ffff00001111"
RUN_ID = "run_" + "1" * 16
TURN_ID = "turn_" + "2" * 16
STEP_ID = "step_" + "3" * 16
TRACE_ID = "b" * 32
CREATED_AT = 1_700_000_000_000


def _native_session(
    directory: Path,
    *,
    title: str = "Native parser fix",
    created_at: int = CREATED_AT,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{NATIVE_ID}.jsonl"
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
            content=(TextPart("please summarize the logs"),),
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


class _V4Info:
    def __init__(
        self,
        session_id: str,
        title: str,
        *,
        cwd: str = "C:/work/v4",
        created_at: int = 1_500_000_000_000,
        updated_at: int = 0,
        turns: int = 2,
        preview: str = "v4 preview",
    ) -> None:
        self.id = session_id
        self.title = title
        self.cwd = cwd
        self.createdAt = created_at
        self.updatedAt = updated_at or created_at
        self.turns = turns
        self.preview = preview


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
    def __init__(self, sessions_dir: Path, v4: list[_V4Info]) -> None:
        self.sessions_dir = sessions_dir
        self.v4 = list(v4)
        self.opened_sessions: list[str] = []
        self.session = SimpleNamespace(session_id=LIVE_ID)

    def recent_sessions(self, limit: int = 20, *, include_archived: bool = False) -> list[_V4Info]:
        return self.v4[:limit]

    def native_sessions(self, limit: int = 20) -> list[object]:
        return list_native_sessions(self.sessions_dir)[:limit]

    async def open_session(self, session_id: str) -> object:
        self.opened_sessions.append(session_id)
        self.session = SimpleNamespace(session_id=session_id)
        return self.session


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


def _host(tmp_path: Path, *, v4: list[_V4Info] | None = None, native: bool = True) -> _Host:
    sessions_dir = tmp_path / "sessions"
    if native:
        _native_session(sessions_dir)
    runtime = _Runtime(sessions_dir, v4 or [])
    return _Host(runtime)


async def test_sessions_lists_v4_and_native_rows_with_a_label(tmp_path: Path) -> None:
    host = _host(tmp_path, v4=[_V4Info(V4_ID, "fix tests", updated_at=1_600_000_000_000)])

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    lines = host.conversation.infos
    assert lines[0] == "sessions (2):"
    native_rows = [line for line in lines if "native · read-only" in line]
    assert len(native_rows) == 1
    assert "Native parser fix" in native_rows[0]
    v4_row = next(line for line in lines if "fix tests" in line)
    assert "native" not in v4_row
    assert lines[-1] == DeepPyroApp.NATIVE_HINT
    assert host.conversation.errors == []


async def test_sessions_orders_native_and_v4_by_recency(tmp_path: Path) -> None:
    host = _host(tmp_path, v4=[_V4Info(V4_ID, "fix tests", updated_at=1_600_000_000_000)])

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    lines = host.conversation.infos
    native_index = next(i for i, line in enumerate(lines) if "Native parser fix" in line)
    v4_index = next(i for i, line in enumerate(lines) if "fix tests" in line)
    assert native_index < v4_index


async def test_resume_native_reads_the_transcript_without_attaching(tmp_path: Path) -> None:
    host = _host(tmp_path)
    runtime = host.runtime
    live = runtime.session

    await DeepPyroApp.resume(host, NATIVE_ID)  # type: ignore[arg-type]

    assert host.conversation.users == ["please summarize the logs"]
    assert host.conversation.assistant == ["the logs look healthy"]
    assert host.conversation.finished == ["the logs look healthy"]
    joined = "\n".join(host.conversation.infos)
    assert "native session" in joined
    assert "read-only in the TUI" in joined
    assert f"dpyro migrate --to v4 --session {NATIVE_ID}" in joined
    assert "still active" in joined
    assert runtime.opened_sessions == []
    assert runtime.session is live
    assert host.conversation.clears == 0
    assert host.renders == []
    assert host.refreshes == 1
    assert host.conversation.errors == []


async def test_resume_native_by_merged_index(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    _native_session(sessions_dir, created_at=CREATED_AT + 100_000)
    runtime = _Runtime(sessions_dir, [_V4Info(V4_ID, "fix tests", updated_at=1_600_000_000_000)])
    host = _Host(runtime)

    await DeepPyroApp.resume(host, "1")  # type: ignore[arg-type]
    assert host.conversation.users == ["please summarize the logs"]
    assert runtime.opened_sessions == []

    await DeepPyroApp.resume(host, "2")  # type: ignore[arg-type]
    assert runtime.opened_sessions == [V4_ID]
    assert host.conversation.clears == 1


async def test_resume_native_matched_by_title_text(tmp_path: Path) -> None:
    host = _host(tmp_path)

    await DeepPyroApp.resume(host, "parser fix")  # type: ignore[arg-type]

    assert host.conversation.users == ["please summarize the logs"]
    assert host.runtime.opened_sessions == []


async def test_resume_picker_labels_native_rows(tmp_path: Path) -> None:
    host = _host(tmp_path)
    host._pick_result = NATIVE_ID

    await DeepPyroApp.resume(host, "")  # type: ignore[arg-type]

    assert host.pick_call is not None
    title, items = host.pick_call
    assert title == "Resume session"
    labels = [label for _value, label, _note in items]
    assert any("native · read-only" in label for label in labels)
    assert host.runtime.opened_sessions == []
    assert host.conversation.users == ["please summarize the logs"]


async def test_v4_only_sessions_listing_is_unchanged(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    runtime = _Runtime(sessions_dir, [_V4Info(V4_ID, "fix tests")])
    host = _Host(runtime)

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    assert host.conversation.infos[0] == "sessions (1):"
    assert host.conversation.infos[-1] == "use /resume <index|id|text> to open one"
    assert all("native" not in line for line in host.conversation.infos)


async def test_v4_resume_still_attaches_the_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    runtime = _Runtime(sessions_dir, [_V4Info(V4_ID, "fix tests")])
    host = _Host(runtime)

    await DeepPyroApp.resume(host, V4_ID)  # type: ignore[arg-type]

    assert runtime.opened_sessions == [V4_ID]
    assert host.conversation.clears == 1
    assert host.renders == ["welcome", "history"]
    assert host.conversation.infos == [f"resumed {short_session_id(V4_ID)}"]


async def test_malformed_native_files_do_not_break_sessions(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "junk.jsonl").write_text("not json\n", encoding="utf-8")
    runtime = _Runtime(sessions_dir, [_V4Info(V4_ID, "fix tests")])
    host = _Host(runtime)

    DeepPyroApp.sessions_command(host, "")  # type: ignore[arg-type]

    assert host.conversation.errors == []
    assert host.conversation.infos[0] == "sessions (1):"
    assert host.conversation.infos[-1] == "use /resume <index|id|text> to open one"


def test_status_layer_lists_native_sessions_from_the_home(tmp_path: Path, monkeypatch: object) -> None:
    home = tmp_path / "home"
    _native_session(home / "sessions")
    monkeypatch.setenv("DEEPPYRO_HOME", str(home))  # type: ignore[attr-defined]
    monkeypatch.delenv("DEEPPYRO_SESSION_DIR", raising=False)  # type: ignore[attr-defined]
    status = object.__new__(StatusLayer)

    infos = StatusLayer.native_sessions(status, 10)

    assert [info.id for info in infos] == [NATIVE_ID]
    assert StatusLayer.native_sessions(status, 0) == []
