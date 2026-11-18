"""Layer 8 of the DeepPyro TUI app: Session lifecycle and transcript rendering layer."""

from __future__ import annotations

from typing import Any, cast

from deeppyro.agent.native_host import NativeSessionHandle
from deeppyro.session.errors import SessionLockedError
from deeppyro.session.metadata import record_archive, record_title
from deeppyro.session.native_index import (
    ARCHIVE_EVENT,
    NativeSessionInfo,
    NativeTranscript,
    read_native_transcript,
)
from deeppyro.tui import native_history
from deeppyro.tui.app_policy import PolicyLayer
from deeppyro.tui.commands import help_text, live_commands
from deeppyro.tui.format import short_session_id
from deeppyro.tui.history import is_ptc_mode_message, process_map
from deeppyro.tui.session_view import (
    listing_lines,
    matches,
    picker_items,
    preview_of,
    relative_time,
    title_of,
)


class SessionLayer(PolicyLayer):
    """Session lifecycle and transcript rendering layer."""

    # Native schema 1 sessions are visible but read-only: the TUI turn engine
    # still runs on v4, so /resume on a native id only renders its transcript.
    NATIVE_LABEL = "native · read-only"
    NATIVE_HINT = "use /resume <index|id|text> to open a v4 session or read a native one (read-only)"
    NATIVE_ATTACH_HINT = "use /resume <index|id|text> to attach a native session; /fork [id] copies one"
    SESSION_HINT = "use /resume <index|id|text> to open one"

    # -- session rendering ----------------------------------------------
    def _render_welcome(self) -> None:
        runtime = self.runtime
        conversation = self.conversation
        conversation.add_info("DeepPyro Harness — python core, terminal UI")
        conversation.add_info(
            f"{runtime.provider_name}/{runtime.model}  ·  agent preset "
            f"{self._agent_preset_display()} ({self._agent_preset()} · "
            f"{self._agent_presentation()} presentation · {self._agent_mode()} mode)  ·  "
            "/model and /provider switch the model; /preset switches the agent preset"
        )
        conversation.add_info("/permission and /cwd change the session settings")
        if self._native_engine():
            conversation.add_info(
                "/sessions lists native sessions; /resume attaches one; /fork copies the current one"
            )
        conversation.add_info("Type a message. /help lists commands. Ctrl+C interrupts, Ctrl+D quits.")

    def _render_history(self) -> None:
        runtime = self.runtime
        session = getattr(runtime, "session", None)
        conversation = self.conversation
        if session is None:
            return
        try:
            messages = session.derive_messages()
        except Exception as exc:  # noqa: BLE001 - a bad log must not brick the UI
            conversation.add_error(f"cannot derive session history: {exc}")
            return
        processes = native_history.process_map(session) if self._native_engine() else process_map(session)
        turn_number = 0
        for message in messages:
            role = str(getattr(message, "role", "") or "")
            text = message.text() if hasattr(message, "text") else ""
            if role == "user":
                if is_ptc_mode_message(message):
                    # PTC image context is recorded as a user message but was
                    # never typed by the user. Skip the bubble and the turn
                    # counter so replayed process blocks keep their recorded
                    # turn numbers.
                    continue
                turn_number += 1
                conversation.add_user(text)
                # Replay the collapsed thinking and tool lines for this turn.
                turn_process = processes.get(turn_number)
                if turn_process is not None:
                    conversation.add_history_process(turn_process)
            elif role == "assistant" and text.strip():
                conversation.stream_text(text)
                conversation.finish_assistant(text)
            elif role == "tool":
                # Tool calls and results are already inside the process block.
                continue

    # -- commands (called by deeppyro.tui.commands) ----------------------
    def command_list(self) -> list[Any] | None:
        """Live command list (plugins included), or None for the fallback."""
        return live_commands(self)

    def show_help(self) -> None:
        self.conversation.add_info(help_text(self.command_list()))

    async def new_session(self) -> None:
        if self._working:
            self.notify("cannot start a session during a turn", severity="warning")
            return
        await self.runtime.new_session()
        self.conversation.clear()
        self._context_used = 0
        self._render_welcome()
        self.refresh_status()

    def _session_items(self, sessions: list[Any]) -> list[tuple[str, str, str]]:
        if not self._has_native(sessions):
            return picker_items(sessions)
        items: list[tuple[str, str, str]] = []
        for info in sessions:
            label = f"{title_of(info)}  ·  {relative_time(self._session_stamp(info))}"
            turns = getattr(info, "turns", 0)
            if turns:
                label += f"  ·  {turns}t"
            if isinstance(info, NativeSessionInfo):
                label += f"  ·  {'native' if self._native_engine() else self.NATIVE_LABEL}"
            note = "  ".join(part for part in (preview_of(info), str(getattr(info, "cwd", "") or "")) if part)
            items.append((str(getattr(info, "id", "") or ""), label, note))
        return items

    @staticmethod
    def _session_stamp(info: Any) -> float:
        for name in ("updatedAt", "updated_at", "createdAt", "created_at"):
            value = getattr(info, name, 0)
            if isinstance(value, (int, float)) and value:
                return float(value)
        return 0.0

    @staticmethod
    def _has_native(sessions: list[Any]) -> bool:
        return any(isinstance(info, NativeSessionInfo) for info in sessions)

    def _native_engine(self) -> bool:
        """Whether this runtime is the native (schema 1) turn engine."""
        return bool(getattr(self.runtime, "native", False))

    def _session_catalogue(self, *, include_archived: bool = False) -> list[Any]:
        """Sessions for the browser: native rows in native mode, merged rows otherwise."""
        if include_archived:
            sessions = list(self.runtime.recent_sessions(limit=50, include_archived=True))
        else:
            sessions = list(self.runtime.recent_sessions(limit=50))
        if self._native_engine():
            return sessions
        return self._merged_sessions(sessions)

    def _native_sessions(self, limit: int = 50) -> list[NativeSessionInfo]:
        lister = getattr(self.runtime, "native_sessions", None)
        if not callable(lister) or limit <= 0:
            return []
        try:
            listed = lister(limit)
        except Exception:  # noqa: BLE001 - a native listing must never break the TUI
            return []
        if not isinstance(listed, list):
            return []
        return [info for info in listed if isinstance(info, NativeSessionInfo)][:limit]

    def _merged_sessions(self, sessions: list[Any]) -> list[Any]:
        native = self._native_sessions()
        if not native:
            return list(sessions)
        merged: dict[str, Any] = {}
        for info in sessions:
            merged.setdefault(str(getattr(info, "id", "") or ""), info)
        for info in native:
            merged.setdefault(info.id, info)
        return sorted(merged.values(), key=self._session_stamp, reverse=True)

    def _listing_rows(self, sessions: list[Any]) -> list[str]:
        if not self._has_native(sessions):
            return listing_lines(sessions)
        lines: list[str] = []
        workspace = ""
        for index, info in enumerate(sessions, start=1):
            cwd = str(getattr(info, "cwd", "") or "")
            if index == 1 or cwd != workspace:
                workspace = cwd
                lines.append(cwd or "(no workspace)")
            parts = [
                f"  {index:>2}. {short_session_id(str(getattr(info, 'id', '') or ''))}",
                title_of(info),
                relative_time(self._session_stamp(info)),
            ]
            turns = getattr(info, "turns", 0)
            if turns:
                parts.append(f"{turns}t")
            if getattr(info, "archived", False):
                parts.append("archived")
            if isinstance(info, NativeSessionInfo):
                parts.append("native" if self._native_engine() else self.NATIVE_LABEL)
            line = "  ".join(parts)
            preview = preview_of(info)
            if preview:
                line += f"  —  {preview}"
            lines.append(line)
        return lines

    def _resume_hint(self, sessions: list[Any]) -> str:
        if self._native_engine():
            return self.NATIVE_ATTACH_HINT
        return self.NATIVE_HINT if self._has_native(sessions) else "use /resume <index|id|text>"

    def _show_native_session(self, info: NativeSessionInfo, *, note: str | None = None) -> None:
        """Render a read-only native transcript (locked, damaged or v4 mode)."""
        conversation = self.conversation
        short = short_session_id(info.id)
        if self._native_engine():
            conversation.add_info(f"native session {short} — schema 1")
        else:
            conversation.add_info(f"native session {short} — schema 1, read-only in the TUI")
        conversation.add_info(
            f"title: {title_of(info)}  ·  workspace: {info.cwd or '(unknown)'}  ·  "
            f"turns: {info.turns}  ·  events: {info.event_count}  ·  "
            f"updated {relative_time(info.updated_at)}"
        )
        try:
            transcript = read_native_transcript(info.id, root=info.path.parent)
        except Exception as exc:  # noqa: BLE001 - a read-only view must not break the live TUI
            conversation.add_error(f"cannot read native session {short}: {exc}")
            transcript = None
        if transcript is not None:
            self._render_native_transcript(transcript)
        if note:
            conversation.add_info(note)
        elif self._native_engine():
            conversation.add_info("this session was not attached; /resume <id> tries again")
        else:
            conversation.add_info(
                "native sessions are read-only in the TUI until the turn engine migrates; "
                f"to continue, run: dpyro migrate --to v4 --session {info.id}"
            )
        live = getattr(self.runtime, "session", None)
        live_id = str(getattr(live, "session_id", "") or "")
        if live_id:
            conversation.add_info(f"live session {short_session_id(live_id)} is still active")
        self.refresh_status()

    def _render_native_transcript(self, transcript: NativeTranscript) -> None:
        conversation = self.conversation
        if not transcript.entries:
            conversation.add_info("(no user/assistant messages recorded)")
        for entry in transcript.entries:
            if entry.role == "user":
                conversation.add_user(entry.text)
            elif entry.role == "assistant":
                if entry.text:
                    conversation.stream_text(entry.text)
                    conversation.finish_assistant(entry.text)
                if entry.tool_names:
                    conversation.add_info("tool calls: " + ", ".join(entry.tool_names))
            elif entry.role == "tool":
                name = entry.tool_names[0] if entry.tool_names else "tool"
                marker = " (error)" if entry.is_error else ""
                conversation.add_info(f"tool {name}{marker}: {entry.text or '(no output)'}")
        for issue in transcript.issues:
            conversation.add_info(f"fold note: {issue}")

    async def resume(self, argument: str) -> None:
        if self._working:
            self.notify("cannot switch session during a turn", severity="warning")
            return
        sessions = self._session_catalogue()
        if not argument:
            if not sessions:
                self.conversation.add_info("no previous sessions")
                return
            choice = await self._pick("Resume session", self._session_items(sessions))
            if choice is None:
                self.conversation.add_info("recent sessions:")
                for line in self._listing_rows(sessions):
                    self.conversation.add_info(line)
                self.conversation.add_info(self._resume_hint(sessions))
                return
            argument = choice
        target = None
        if argument.isdigit():
            index = int(argument)
            if 1 <= index <= len(sessions):
                target = sessions[index - 1]
        else:
            bare = argument.removeprefix("session-")
            for info in sessions:
                info_id = str(getattr(info, "id", "") or "")
                if info_id == argument or info_id.removeprefix("session-").startswith(bare):
                    target = info
                    break
        if target is None:
            candidates = [info for info in sessions if matches(info, argument)]
            if len(candidates) == 1:
                target = candidates[0]
            elif candidates:
                self.conversation.add_info(f"{len(candidates)} sessions match {argument!r}:")
                for line in self._listing_rows(candidates[:20]):
                    self.conversation.add_info(line)
                self.conversation.add_info("use /resume <index|id>")
                return
        if target is None:
            self.conversation.add_error(f"session not found: {argument}")
            return
        if isinstance(target, NativeSessionInfo):
            session_id = str(getattr(target, "id", "") or "")
            if not self._native_engine():
                # Read-only: never attach a schema 1 session to the live v4 runtime.
                self._show_native_session(target)
                return
            if not getattr(target, "readable", True):
                self._show_native_session(
                    target,
                    note="this session log did not fold cleanly; showing a read-only transcript",
                )
                return
            try:
                await self.runtime.open_session(session_id)
            except SessionLockedError:
                self._show_native_session(
                    target,
                    note="another process owns this session; showing a read-only transcript",
                )
                return
            except Exception as exc:  # noqa: BLE001 - a bad log must stay readable
                self._show_native_session(
                    target,
                    note=f"cannot attach this session: {exc}; showing a read-only transcript",
                )
                return
            self._show_resumed(session_id)
            return
        session_id = str(getattr(target, "id", "") or "")
        await self.runtime.open_session(session_id)
        self._show_resumed(session_id)

    def _show_resumed(self, session_id: str) -> None:
        """Re-render the conversation for an attached session."""
        self.conversation.clear()
        self._context_used = 0
        self._render_welcome()
        self.conversation.add_info(f"resumed {short_session_id(session_id)}")
        self._render_history()
        self.refresh_status()

    def sessions_command(self, argument: str = "") -> None:
        """List sessions without opening one: title, age, turns and a preview."""
        raw = (argument or "").strip()
        include_archived = raw in {"--all", "-a"}
        if include_archived:
            raw = ""
        sessions = self._session_catalogue(include_archived=include_archived)
        if raw:
            sessions = [info for info in sessions if matches(info, raw)]
        if not sessions:
            detail = f" matching {raw!r}" if raw else ""
            self.conversation.add_info(f"no sessions{detail}")
            return
        header = "archived sessions" if include_archived else "sessions"
        if raw:
            header += f" matching {raw!r}"
        self.conversation.add_info(f"{header} ({len(sessions)}):")
        for line in self._listing_rows(sessions):
            self.conversation.add_info(line)
        if self._native_engine():
            self.conversation.add_info(self.NATIVE_ATTACH_HINT)
        else:
            hint = self.NATIVE_HINT if self._has_native(sessions) else self.SESSION_HINT
            self.conversation.add_info(hint)

    def rename_command(self, argument: str = "") -> None:
        """Give the current session a title that survives reload and /resume."""
        title = (argument or "").strip()
        if not title:
            self.conversation.add_info("usage: /rename <title>")
            return
        session = getattr(self.runtime, "session", None)
        if session is None:
            self.conversation.add_error("no active session")
            return
        if self._native_engine():
            append = getattr(session, "append", None)
            if not callable(append):
                self.conversation.add_error("this native session cannot record a title")
                return
            try:
                append("session/title", {"title": title})
                view = session.fold()
                stored = str(getattr(view.state, "title", "") or title)
            except Exception as exc:  # noqa: BLE001 - a rename must not break the UI
                self.conversation.add_error(f"could not rename: {exc}")
                return
            self.conversation.add_info(f"renamed this session to {stored!r}")
            self.refresh_status()
            return
        try:
            stored = record_title(session, title)
        except Exception as exc:  # noqa: BLE001 - a rename must not break the UI
            self.conversation.add_error(f"could not rename: {exc}")
            return
        self.conversation.add_info(f"renamed this session to {stored!r}")
        self.refresh_status()

    async def archive_command(self) -> None:
        """Hide the current session from lists and start a fresh one."""
        if self._working:
            self.notify("cannot archive during a turn", severity="warning")
            return
        session = getattr(self.runtime, "session", None)
        if session is None:
            self.conversation.add_error("no active session")
            return
        session_id = short_session_id(str(getattr(session, "session_id", "") or ""))
        if self._native_engine():
            native_session = getattr(session, "native_session", None)
            appender = getattr(native_session, "append", None)
            if not callable(appender):
                self.conversation.add_error("this native session cannot be archived")
                return
            try:
                appender(ARCHIVE_EVENT, {"archived": True, "source": "user"})
            except Exception as exc:  # noqa: BLE001 - archiving must not break the UI
                self.conversation.add_error(f"could not archive: {exc}")
                return
            await self.new_session()
            self.conversation.add_info(f"archived {session_id}; started a new session")
            self.conversation.add_info("use /sessions --all to see archived sessions, /unarchive to restore")
            return
        try:
            record_archive(session, True)
        except Exception as exc:  # noqa: BLE001 - archiving must not break the UI
            self.conversation.add_error(f"could not archive: {exc}")
            return
        await self.new_session()
        self.conversation.add_info(f"archived {session_id}; started a new session")
        self.conversation.add_info("use /sessions --all to see archived sessions, /unarchive to restore")

    def unarchive_command(self, argument: str = "") -> None:
        """Restore an archived session so it shows up in /sessions again."""
        archived = self.runtime.archived_sessions(limit=50)
        if not archived:
            self.conversation.add_info("no archived sessions")
            return
        raw = (argument or "").strip()
        target = None
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(archived):
                target = archived[index - 1]
        elif raw:
            candidates = [info for info in archived if matches(info, raw)]
            bare = raw.removeprefix("session-")
            for info in candidates:
                info_id = str(getattr(info, "id", "") or "")
                if info_id == raw or info_id.removeprefix("session-").startswith(bare):
                    target = info
                    break
            if target is None and len(candidates) == 1:
                target = candidates[0]
        if target is None:
            self.conversation.add_info("archived sessions:")
            rows = self._listing_rows(archived) if self._native_engine() else listing_lines(archived)
            for line in rows:
                self.conversation.add_info(line)
            self.conversation.add_info("use /unarchive <index|id|text>")
            return
        session_id = str(getattr(target, "id", "") or "")
        if self._native_engine():
            archiver = getattr(getattr(self.runtime, "sessions", None), "native_archive", None)
            if not callable(archiver):
                self.conversation.add_error("unarchiving is not available in this runtime")
                return
            try:
                archiver(session_id, archived=False)
            except Exception as exc:  # noqa: BLE001 - unarchiving must not break the UI
                self.conversation.add_error(f"could not unarchive: {exc}")
                return
            self.conversation.add_info(
                f"restored {short_session_id(session_id)}; use /resume {session_id} to open it"
            )
            return
        try:
            session = self.runtime.store.open(session_id)
            record_archive(session, False)
        except Exception as exc:  # noqa: BLE001 - unarchiving must not break the UI
            self.conversation.add_error(f"could not unarchive: {exc}")
            return
        self.conversation.add_info(
            f"restored {short_session_id(session_id)}; use /resume {session_id} to open it"
        )

    async def fork_command(self, argument: str = "") -> None:
        """Copy the current (or named) native session into a fresh session (/fork)."""
        if self._working:
            self.notify("cannot fork during a turn", severity="warning")
            return
        if not self._native_engine():
            self.conversation.add_info("/fork requires the native TUI runtime (DEEPPYRO_TUI_NATIVE=1)")
            return
        fork = getattr(getattr(self.runtime, "sessions", None), "native_fork", None)
        if not callable(fork):
            self.conversation.add_error("forking is not available in this runtime")
            return
        source = (argument or "").strip()
        if source.isdigit():
            catalogue = self._session_catalogue()
            index = int(source)
            source = (
                str(getattr(catalogue[index - 1], "id", "") or "") if 1 <= index <= len(catalogue) else ""
            )
        source = source or str(getattr(self.runtime, "session_id", "") or "")
        if source and not source.startswith("session-"):
            source = f"session-{source}"
        if not source:
            self.conversation.add_error("no active session to fork")
            return
        try:
            forked = cast(Any, fork(source))
        except Exception as exc:  # noqa: BLE001 - a fork failure must stay visible
            self.conversation.add_error(f"fork failed: {exc}")
            return
        handle = NativeSessionHandle(forked)
        new_id = handle.session_id
        try:
            await self.runtime._detach()
            handle.aclose()
            await self.runtime.open_session(new_id)
        except Exception as exc:  # noqa: BLE001 - a fork failure must stay visible
            self.conversation.add_error(f"fork failed: {exc}")
            return
        self.conversation.clear()
        self._context_used = 0
        self._render_welcome()
        self.conversation.add_info(f"forked {short_session_id(source)} -> {short_session_id(new_id)}")
        self._render_history()
        self.refresh_status()
