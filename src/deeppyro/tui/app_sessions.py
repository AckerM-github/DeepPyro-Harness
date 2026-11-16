"""Layer 8 of the DeepPyro TUI app: Session lifecycle and transcript rendering layer."""

from __future__ import annotations

from typing import Any

from deeppyro.session.metadata import record_archive, record_title
from deeppyro.tui.app_policy import PolicyLayer
from deeppyro.tui.commands import help_text, live_commands
from deeppyro.tui.format import short_session_id
from deeppyro.tui.history import is_ptc_mode_message, process_map
from deeppyro.tui.session_view import listing_lines, matches, picker_items


class SessionLayer(PolicyLayer):
    """Session lifecycle and transcript rendering layer."""

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
        processes = process_map(session)
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
        return picker_items(sessions)

    async def resume(self, argument: str) -> None:
        if self._working:
            self.notify("cannot switch session during a turn", severity="warning")
            return
        sessions = self.runtime.recent_sessions(limit=50)
        if not argument:
            if not sessions:
                self.conversation.add_info("no previous sessions")
                return
            choice = await self._pick("Resume session", self._session_items(sessions))
            if choice is None:
                self.conversation.add_info("recent sessions:")
                for line in listing_lines(sessions):
                    self.conversation.add_info(line)
                self.conversation.add_info("use /resume <index|id|text>")
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
                for line in listing_lines(candidates[:20]):
                    self.conversation.add_info(line)
                self.conversation.add_info("use /resume <index|id>")
                return
        if target is None:
            self.conversation.add_error(f"session not found: {argument}")
            return
        session_id = str(getattr(target, "id", "") or "")
        await self.runtime.open_session(session_id)
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
        sessions = self.runtime.recent_sessions(limit=50, include_archived=include_archived)
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
        for line in listing_lines(sessions):
            self.conversation.add_info(line)
        self.conversation.add_info("use /resume <index|id|text> to open one")

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
            for line in listing_lines(archived):
                self.conversation.add_info(line)
            self.conversation.add_info("use /unarchive <index|id|text>")
            return
        session_id = str(getattr(target, "id", "") or "")
        try:
            session = self.runtime.store.open(session_id)
            record_archive(session, False)
        except Exception as exc:  # noqa: BLE001 - unarchiving must not break the UI
            self.conversation.add_error(f"could not unarchive: {exc}")
            return
        self.conversation.add_info(
            f"restored {short_session_id(session_id)}; use /resume {session_id} to open it"
        )
