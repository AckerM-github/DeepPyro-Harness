"""The chat stage: stage header, transcript, streaming draft, tool and approval cards, and the composer.

ChatView is a plain Python component. It owns no HTML: every node is a NiceGUI element,
every dynamic string goes through ui.label or ui.markdown, and all styling lives in
assets/chat.css. The component keeps a small state machine so that SSE deltas, the
polling safety net and user actions can never rebuild the transcript concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from nicegui import ui

from deeppyro.web.ui import theme
from deeppyro.web.ui.bridge import bridge_error
from deeppyro.web.ui.commands import STATUS_ERROR, STATUS_OK, STATUS_REDIRECT

if TYPE_CHECKING:
    from deeppyro.web.ui.bridge import WebUiBridge

__all__ = [
    "ChatView",
    "DraftState",
    "MessageState",
    "StreamAction",
    "TimelineState",
    "ToolCallState",
    "ToolStatus",
    "append_delta",
    "format_tool_arguments",
    "header_status",
    "normalise_approvals",
    "normalise_message",
    "normalise_timeline",
    "parse_sse_frame",
    "source_badge",
    "split_slash_command",
    "stream_action",
    "timeline_signature",
    "tool_status",
    "tool_status_tone",
]

#: Author label used for the user's own rows.
USER_NAME = "You"
#: Stable accent colour for the user avatar (theme.avatar paints the disc).
USER_COLOUR = "#7c6cf2"
#: Author label used for assistant rows.
ASSISTANT_NAME = "DeepPyro"

#: Polling safety net: rebuild the transcript only when the timeline changed.
POLL_INTERVAL = 0.8
#: Maximum size of the in-memory streaming draft.
DRAFT_LIMIT = 60_000
#: Maximum characters rendered for one tool argument/result block.
TOOL_BLOCK_LIMIT = 6_000
#: Maximum characters rendered for an approval detail block.
APPROVAL_DETAIL_LIMIT = 1_200
#: Scroll percentages above this keep the view pinned to the newest message.
SCROLL_STICK_THRESHOLD = 0.97
#: How long a draft may survive without new deltas once the turn is no longer busy.
DRAFT_IDLE_GRACE = 3.0
#: Minimum time between two draft repaints (deltas are cheap, DOM updates are not).
DRAFT_PAINT_INTERVAL = 0.05
#: Minimum time between two programmatic scrolls.
SCROLL_INTERVAL = 0.15


class ToolStatus(StrEnum):
    """Rendered state of one tool call."""

    PENDING = "pending"
    DONE = "done"
    ERROR = "error"


class StreamAction(StrEnum):
    """What the chat stage should do with one SSE event."""

    DRAFT_TEXT = "draft-text"
    DRAFT_THINKING = "draft-thinking"
    FINAL = "final"
    REFRESH = "refresh"
    IGNORE = "ignore"


@dataclass(frozen=True, slots=True)
class ToolCallState:
    """One tool call as joined into an assistant timeline message."""

    call_id: str = ""
    name: str = ""
    arguments: str = ""
    result: str | None = None
    is_error: bool = False
    pending: bool = False

    @property
    def status(self) -> ToolStatus:
        return tool_status(pending=self.pending, is_error=self.is_error, result=self.result)


@dataclass(frozen=True, slots=True)
class MessageState:
    """One persisted timeline message."""

    message_id: str = ""
    role: str = "assistant"
    created_at: Any = None
    text: str = ""
    reasoning: str = ""
    source_kind: str = ""
    tool_call_id: str = ""
    is_error: bool = False
    has_text: bool = False
    images: tuple[str, ...] = ()
    tool_calls: tuple[ToolCallState, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalState:
    """One pending approval prompt."""

    approval_id: str = ""
    session_id: str = ""
    tool: str = ""
    summary: str = ""
    detail: str = ""
    created_at: Any = None


@dataclass(frozen=True, slots=True)
class TimelineState:
    """Normalised timeline payload; the error field carries producer errors as data."""

    session_id: str = ""
    title: str = ""
    cwd: str = ""
    active: bool = False
    busy: bool = False
    pending: int = 0
    readable: bool = True
    archived: bool = False
    provider: str = ""
    model: str = ""
    preset: str = ""
    permission_preset: str = ""
    context_tokens: int = 0
    context_window: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    journal_seq: int = 0
    messages: tuple[MessageState, ...] = ()
    error: str = ""


@dataclass(slots=True)
class DraftState:
    """Assistant text that arrived over SSE but is not part of the timeline yet."""

    text: str = ""
    reasoning: str = ""
    base_seq: int = 0
    settle_seq: int | None = None

    @property
    def has_content(self) -> bool:
        return bool(self.text or self.reasoning)

    def add_text(self, delta: str, *, seq: int = 0) -> None:
        if not delta:
            return
        if seq and not self.base_seq:
            self.base_seq = seq
        self.text = append_delta(self.text, delta)

    def add_reasoning(self, delta: str, *, seq: int = 0) -> None:
        if not delta:
            return
        if seq and not self.base_seq:
            self.base_seq = seq
        self.reasoning = append_delta(self.reasoning, delta)

    def finish(self, text: str, *, seq: int = 0) -> None:
        if seq and not self.base_seq:
            self.base_seq = seq
        if text:
            self.text = text
        elif not self.text:
            return
        self.settle_seq = seq or self.base_seq


def _as_text(value: Any) -> str:
    """Coerce an arbitrary producer value into display text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(item) for item in value)
    if isinstance(value, Mapping):
        try:
            return json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def format_tool_block(value: Any, *, limit: int = TOOL_BLOCK_LIMIT) -> str:
    """Render a tool argument/result value for a monospace block, truncated from the tail."""
    text = _as_text(value)
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n… ({len(text) - limit} more characters)"


def format_tool_arguments(value: Any) -> str:
    """Pretty-print JSON arguments; leave free-form strings and non-JSON values untouched."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return format_tool_block(json.loads(stripped))
            except (TypeError, ValueError):
                pass
        return format_tool_block(value)
    return format_tool_block(value)


def tool_status(*, pending: bool, is_error: bool, result: str | None) -> ToolStatus:
    """Map producer flags onto the one rendered status badge."""
    if pending or result is None:
        return ToolStatus.PENDING
    if is_error:
        return ToolStatus.ERROR
    return ToolStatus.DONE


def tool_status_tone(status: ToolStatus) -> str:
    if status is ToolStatus.DONE:
        return "ok"
    if status is ToolStatus.ERROR:
        return "danger"
    return "warn"


_SOURCE_BADGES: dict[str, tuple[str, str]] = {
    "provider": ("AGENT", "muted"),
    "plugin": ("PLUGIN", "warn"),
    "goal": ("GOAL", "ok"),
    "repair": ("REPAIR", "danger"),
}


def source_badge(kind: Any) -> tuple[str, str] | None:
    """Map message.source.kind onto an optional (label, tone) badge."""
    return _SOURCE_BADGES.get(_as_text(kind).strip().lower())


def header_status(timeline: TimelineState) -> tuple[str, str]:
    """Return the header status label and badge tone for the current timeline."""
    if timeline.error:
        return "UNAVAILABLE", "danger"
    if not timeline.session_id:
        return "NO SESSION", "muted"
    if timeline.archived:
        return "ARCHIVED", "muted"
    if not timeline.active:
        return "READ ONLY", "warn"
    if timeline.busy:
        return "WORKING", "ok"
    return "IDLE", "muted"


def normalise_tool_call(raw: Any) -> ToolCallState | None:
    """Normalise one tool_calls entry; None when the entry is not an object."""
    if not isinstance(raw, Mapping):
        return None
    result = raw.get("result")
    result_text = _as_text(result) if result is not None else None
    pending = bool(raw.get("pending")) or result_text is None
    return ToolCallState(
        call_id=_as_text(raw.get("id")),
        name=_as_text(raw.get("name")) or "tool",
        arguments=format_tool_arguments(raw.get("arguments")),
        result=result_text,
        is_error=bool(raw.get("is_error")),
        pending=pending,
    )


def normalise_message(raw: Any) -> MessageState | None:
    """Normalise one timeline message; None when the entry is not an object."""
    if not isinstance(raw, Mapping):
        return None
    source = raw.get("source")
    source_kind = _as_text(source.get("kind")) if isinstance(source, Mapping) else ""
    tool_calls: list[ToolCallState] = []
    for item in _as_list(raw.get("tool_calls")):
        call = normalise_tool_call(item)
        if call is not None:
            tool_calls.append(call)
    images: list[str] = []
    for item in _as_list(raw.get("images")):
        if isinstance(item, Mapping):
            attachment = _as_text(item.get("attachment"))
            if attachment:
                images.append(attachment)
    text = _as_text(raw.get("text"))
    has_text = raw.get("has_text")
    return MessageState(
        message_id=_as_text(raw.get("id")),
        role=_as_text(raw.get("role")) or "assistant",
        created_at=raw.get("created_at"),
        text=text,
        reasoning=_as_text(raw.get("reasoning")),
        source_kind=source_kind,
        tool_call_id=_as_text(raw.get("tool_call_id")),
        is_error=bool(raw.get("is_error")),
        has_text=bool(has_text) if has_text is not None else bool(text),
        images=tuple(images),
        tool_calls=tuple(tool_calls),
    )


def normalise_messages(raw_messages: Any) -> tuple[MessageState, ...]:
    """Normalise the whole timeline message list, dropping unknown entries."""
    messages: list[MessageState] = []
    for item in _as_list(raw_messages):
        message = normalise_message(item)
        if message is not None:
            messages.append(message)
    return tuple(messages)


def normalise_timeline(payload: Any) -> TimelineState:
    """Normalise a timeline payload; errors are data, never exceptions."""
    if not isinstance(payload, Mapping):
        return TimelineState(error="timeline payload is not an object")
    usage_raw = payload.get("usage")
    usage = (
        {str(key): _as_int(value) for key, value in usage_raw.items()}
        if isinstance(usage_raw, Mapping)
        else {}
    )
    return TimelineState(
        session_id=_as_text(payload.get("session_id")),
        title=_as_text(payload.get("title")),
        cwd=_as_text(payload.get("cwd")),
        active=bool(payload.get("active")),
        busy=bool(payload.get("busy")),
        pending=_as_int(payload.get("pending")),
        readable=bool(payload.get("readable", True)),
        archived=bool(payload.get("archived")),
        provider=_as_text(payload.get("provider")),
        model=_as_text(payload.get("model")),
        preset=_as_text(payload.get("preset")),
        permission_preset=_as_text(payload.get("permission_preset")),
        context_tokens=_as_int(payload.get("context_tokens")),
        context_window=_as_int(payload.get("context_window")),
        usage=usage,
        journal_seq=_as_int(payload.get("journal_seq")),
        messages=normalise_messages(payload.get("messages")),
        error=_as_text(payload.get("error")),
    )


def normalise_approvals(rows: Any) -> tuple[ApprovalState, ...]:
    """Normalise the approvals list; rows without an id are dropped."""
    approvals: list[ApprovalState] = []
    for raw in _as_list(rows):
        if not isinstance(raw, Mapping):
            continue
        approval_id = _as_text(raw.get("approval_id"))
        if not approval_id:
            continue
        approvals.append(
            ApprovalState(
                approval_id=approval_id,
                session_id=_as_text(raw.get("session_id")),
                tool=_as_text(raw.get("tool")) or "tool",
                summary=_as_text(raw.get("summary")),
                detail=format_tool_block(raw.get("detail"), limit=APPROVAL_DETAIL_LIMIT),
                created_at=raw.get("created_at"),
            )
        )
    return tuple(approvals)


def timeline_signature(
    timeline: TimelineState,
    approvals: Iterable[ApprovalState] = (),
    *,
    notice: tuple[str, str] | None = None,
    stream_error: str = "",
) -> tuple[Any, ...]:
    """Cheap signature for the whole rendered state; rebuild only when it changes."""
    messages = tuple(
        (
            message.message_id,
            message.role,
            message.created_at,
            message.text,
            message.reasoning,
            message.source_kind,
            message.is_error,
            message.images,
            tuple(
                (call.call_id, call.name, call.arguments, call.result, call.is_error, call.pending)
                for call in message.tool_calls
            ),
        )
        for message in timeline.messages
    )
    approval_signature = tuple(
        (approval.approval_id, approval.session_id, approval.tool, approval.summary, approval.detail)
        for approval in approvals
    )
    return (
        timeline.session_id,
        timeline.title,
        timeline.cwd,
        timeline.active,
        timeline.busy,
        timeline.pending,
        timeline.readable,
        timeline.archived,
        timeline.provider,
        timeline.model,
        timeline.preset,
        timeline.context_tokens,
        timeline.context_window,
        timeline.journal_seq,
        timeline.error,
        messages,
        approval_signature,
        notice,
        stream_error,
    )


def parse_sse_frame(frame: Any) -> dict[str, Any] | None:
    """Parse one SSE frame (id: N / data: {...} / blank line) into the wire event dict.

    Heartbeats, malformed JSON and non object payloads all return None.
    """
    if isinstance(frame, (bytes, bytearray)):
        try:
            text = bytes(frame).decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(frame, str):
        text = frame
    else:
        return None
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    if not data_lines:
        return None
    try:
        event = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict):
        return None
    return event


def stream_action(event: Mapping[str, Any] | None) -> StreamAction:
    """Decide how the SSE consumer handles one event."""
    if not isinstance(event, Mapping):
        return StreamAction.IGNORE
    kind = _as_text(event.get("type")).strip().lower()
    if kind in ("text", "thinking", "final") and not _as_text(event.get("text")):
        return StreamAction.IGNORE
    if kind == "text":
        return StreamAction.DRAFT_TEXT
    if kind == "thinking":
        return StreamAction.DRAFT_THINKING
    if kind == "final":
        return StreamAction.FINAL
    if kind in {"status", "step", "tool_call", "tool_result", "approval", "usage", "error", "session"}:
        return StreamAction.REFRESH
    return StreamAction.IGNORE


def append_delta(current: str, delta: str, *, limit: int = DRAFT_LIMIT) -> str:
    """Append a streamed delta, keeping only the newest limit characters."""
    if not delta:
        return current
    combined = f"{current}{delta}"
    if len(combined) <= limit:
        return combined
    return combined[-limit:]


def split_slash_command(text: str) -> tuple[str, list[str]] | None:
    """Split slash-command text into the command name and its arguments."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    try:
        parts = shlex.split(stripped)
    except ValueError:
        parts = stripped.split()
    if not parts or parts[0] == "/":
        return None
    return parts[0], parts[1:]


def _settle_task(task: asyncio.Task[Any]) -> None:
    """Consume a finished background task so asyncio never warns about lost exceptions."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.exception()


class ChatView:
    """The chat stage for one session, mounted inside layout.py's div.dp-stage."""

    def __init__(
        self,
        bridge: WebUiBridge,
        session_id: str | None,
        *,
        on_session_change: Callable[[str], None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._session_id = session_id or bridge.active_session_id
        self._on_session_change = on_session_change
        self._timeline = TimelineState()
        self._approvals: tuple[ApprovalState, ...] = ()
        self._draft = DraftState()
        self._notice: tuple[str, str] | None = None
        self._stream_error = ""
        self._signature: tuple[Any, ...] | None = None
        self._last_seq = 0
        self._draft_updated_at = 0.0
        self._draft_painted_at = 0.0
        self._scroll_at = 0.0
        self._stick_to_bottom = True
        self._sending = False
        self._deciding = False
        self._creating = False
        self._stopped = False
        self._stream_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._refresh_lock = asyncio.Lock()
        self._poll_timer: Any = None
        self._root: ui.element | None = None
        self._head: ui.element | None = None
        self._messages_area: Any = None
        self._message_list: ui.element | None = None
        self._draft_row: ui.element | None = None
        self._draft_status: ui.label | None = None
        self._draft_text: ui.label | None = None
        self._draft_reasoning: Any = None
        self._draft_reasoning_body: ui.label | None = None
        self._approvals_slot: ui.element | None = None
        self._input: Any = None
        self._send_btn: Any = None
        self._hint: ui.label | None = None
        self._composer_status: ui.label | None = None

    # -- public API -------------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        """The session currently rendered by the stage."""
        return self._session_id

    async def mount(self, parent: ui.element) -> None:
        """Build the chat stage inside parent and start streaming + polling."""
        theme.install_head()
        with parent, ui.element("div").classes("dp-chat") as root:
            self._root = root
            self._build_head()
            self._build_messages()
            self._build_approvals()
            self._build_composer()
            self._poll_timer = ui.timer(POLL_INTERVAL, self._poll, immediate=False)
        self._install_lifecycle()
        await self._refresh(force=True)
        self._start_stream()

    # -- construction -----------------------------------------------------------

    def _build_head(self) -> None:
        with ui.element("div").classes("dp-stage-head") as head:
            self._head = head

    def _build_messages(self) -> None:
        self._messages_area = ui.scroll_area(on_scroll=self._on_scroll).classes("dp-messages")
        with self._messages_area, ui.element("div").classes("dp-thread"):
            with ui.element("div").classes("dp-message-list") as message_list:
                self._message_list = message_list
            self._build_draft()

    def _build_draft(self) -> None:
        with ui.element("div").classes("dp-draft") as draft_row:
            self._draft_row = draft_row
            with ui.element("div").classes("dp-message dp-message-assistant dp-draft-message"):
                self._paint_brand_avatar()
                with ui.element("div").classes("dp-message-main"):
                    with ui.element("div").classes("dp-message-head"):
                        ui.label(ASSISTANT_NAME).classes("dp-message-name")
                        self._draft_status = ui.label("STREAMING").classes("dp-draft-status")
                    with ui.element("div").classes("dp-message-body"):
                        self._draft_reasoning = ui.expansion("Reasoning", icon=theme.ICONS["spark"]).classes(
                            "dp-reasoning"
                        )
                        with self._draft_reasoning:
                            self._draft_reasoning_body = ui.label("").classes("dp-reasoning-body")
                        self._draft_reasoning.set_visibility(False)
                        with ui.element("div").classes("dp-draft-body"):
                            self._draft_text = ui.label("").classes("dp-draft-text")
            draft_row.set_visibility(False)

    def _build_approvals(self) -> None:
        with ui.element("div").classes("dp-approvals") as slot:
            self._approvals_slot = slot
            slot.set_visibility(False)

    def _build_composer(self) -> None:
        with ui.element("div").classes("dp-composer"):
            with ui.element("div").classes("dp-composer-box"):
                self._input = (
                    ui.textarea(placeholder="Message DeepPyro — / for commands")
                    .props("autogrow borderless dense")
                    .classes("dp-composer-input")
                )
                self._input.on("keydown.enter.exact", self._send_current, [])
                self._send_btn = (
                    ui.button(icon=theme.ICONS["send"], on_click=self._send_current)
                    .props("flat round dense")
                    .classes("dp-send-btn")
                    .tooltip("Send (Enter)")
                )
            with ui.element("div").classes("dp-composer-foot"):
                self._hint = ui.label("").classes("dp-composer-hint")
                self._composer_status = ui.label("").classes("dp-composer-status")
        self._paint_composer()

    def _install_lifecycle(self) -> None:
        with contextlib.suppress(RuntimeError):
            client = ui.context.client
            client.on_disconnect(self._on_disconnect)

    # -- lifecycle / navigation -------------------------------------------------

    def _on_disconnect(self, _client: Any = None) -> None:
        """Tear down background work when this page goes away (also fired on reconnect)."""
        self._stopped = True
        if self._stream_task is not None:
            self._stream_task.cancel()
            self._stream_task = None
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
        if self._poll_timer is not None:
            with contextlib.suppress(Exception):
                self._poll_timer.cancel()
            self._poll_timer = None

    def _navigate(self, path: str) -> None:
        if not path:
            return
        if self._on_session_change is not None:
            self._on_session_change(path)
            return
        with contextlib.suppress(RuntimeError):
            ui.navigate.to(path)

    def _navigate_session(self, session_id: str) -> None:
        self._navigate(f"/c/{session_id}")

    def _open_settings(self) -> None:
        self._navigate("/settings")

    def _open_palette(self) -> None:
        self._navigate("/palette")

    # -- data loading -----------------------------------------------------------

    async def _load(self) -> tuple[TimelineState, tuple[ApprovalState, ...]]:
        session_id = self._session_id or self._bridge.active_session_id
        try:
            payload = await self._bridge.timeline(session_id)
        except Exception as exc:  # noqa: BLE001 - the bridge promises not to raise; stay safe
            return TimelineState(session_id=session_id or "", error=bridge_error(exc)), ()
        timeline = normalise_timeline(payload)
        target = timeline.session_id or session_id
        approvals: tuple[ApprovalState, ...] = ()
        if target:
            try:
                rows = await self._bridge.approvals(target)
            except Exception:  # noqa: BLE001 - approvals are best effort; the poll retries
                approvals = self._approvals
            else:
                approvals = normalise_approvals(rows)
        return timeline, approvals

    async def _refresh(self, *, force: bool = False) -> None:
        """Re-read timeline + approvals and repaint only when the rendered state changed."""
        if self._stopped:
            return
        async with self._refresh_lock:
            if self._stopped:
                return
            timeline, approvals = await self._load()
            signature = timeline_signature(
                timeline,
                approvals,
                notice=self._notice,
                stream_error=self._stream_error,
            )
            if not force and signature == self._signature:
                return
            self._signature = signature
            self._timeline = timeline
            self._approvals = approvals
            if timeline.session_id and timeline.session_id != self._session_id:
                self._session_id = timeline.session_id
            self._last_seq = max(self._last_seq, timeline.journal_seq)
            self._settle_draft(timeline)
            self._paint()

    def _settle_draft(self, timeline: TimelineState) -> None:
        """Drop the streaming draft once the persisted timeline holds the same turn."""
        draft = self._draft
        if not draft.has_content:
            return
        if draft.settle_seq is not None and timeline.journal_seq >= draft.settle_seq:
            self._draft = DraftState()
            return
        idle = time.monotonic() - self._draft_updated_at
        if (
            not timeline.busy
            and draft.base_seq
            and timeline.journal_seq > draft.base_seq
            and idle > DRAFT_IDLE_GRACE
        ):
            self._draft = DraftState()

    async def _poll(self) -> None:
        if self._stopped:
            return
        self._flush_draft()
        await self._refresh()

    # -- SSE --------------------------------------------------------------------

    def _start_stream(self) -> None:
        if self._stopped or not self._session_id:
            return
        if self._stream_task is not None and not self._stream_task.done():
            return
        self._stream_error = ""
        self._stream_task = asyncio.create_task(self._consume(), name="dp-chat-sse")
        self._stream_task.add_done_callback(_settle_task)

    async def _consume(self) -> None:
        """Consume bridge.sse until cancelled or the producer fails."""
        session_id = self._session_id
        if not session_id:
            return
        try:
            async for frame in self._bridge.sse(session_id, after=self._last_seq):
                if self._stopped:
                    break
                event = parse_sse_frame(frame)
                if event is None:
                    continue
                event_session = _as_text(event.get("session_id"))
                if event_session and event_session != session_id:
                    continue
                self._last_seq = max(self._last_seq, _as_int(event.get("seq")))
                self._handle_stream_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface the failure, keep polling
            if not self._stopped:
                self._stream_error = bridge_error(exc)
                self._flush_draft(force=True)
                self._kick_refresh()

    def _handle_stream_event(self, event: Mapping[str, Any]) -> None:
        action = stream_action(event)
        if action is StreamAction.IGNORE:
            return
        if action is StreamAction.REFRESH:
            self._kick_refresh()
            return
        seq = _as_int(event.get("seq"))
        if action is StreamAction.FINAL:
            self._draft.finish(_as_text(event.get("text")), seq=seq)
            self._draft_updated_at = time.monotonic()
            self._flush_draft(force=True)
            self._kick_refresh()
            return
        delta = _as_text(event.get("text"))
        if action is StreamAction.DRAFT_TEXT:
            self._draft.add_text(delta, seq=seq)
        else:
            self._draft.add_reasoning(delta, seq=seq)
        self._draft_updated_at = time.monotonic()
        self._flush_draft()

    def _kick_refresh(self) -> None:
        """Coalesce refresh requests: at most one background refresh in flight."""
        if self._stopped:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.create_task(self._refresh(), name="dp-chat-refresh")
        self._refresh_task.add_done_callback(_settle_task)

    # -- painting ---------------------------------------------------------------

    def _paint(self) -> None:
        if self._root is None or self._stopped:
            return
        try:
            self._paint_head()
            self._paint_messages()
            self._paint_approvals()
            self._paint_composer()
            self._flush_draft(force=True)
        except RuntimeError:
            self._stopped = True
            return
        self._scroll_to_bottom(force=True)

    def _head_title(self) -> str:
        title = self._timeline.title.strip()
        if title:
            return title
        if self._session_id:
            return "(untitled session)"
        return "No session"

    def _paint_head(self) -> None:
        head = self._head
        if head is None:
            return
        timeline = self._timeline
        status_text, status_tone = header_status(timeline)
        head.clear()
        with head:
            with ui.element("div").classes("dp-head-main"):
                with ui.element("div").classes("dp-head-title-row"):
                    theme.icon("spark", size="16px", colour="var(--dp-accent)")
                    ui.label(self._head_title()).classes("dp-head-title")
                    if timeline.archived:
                        theme.badge("ARCHIVED", tone="muted")
                with ui.element("div").classes("dp-head-meta"):
                    theme.pill(timeline.preset or "standard", icon_name="bolt", tone="muted")
                    model_text = (
                        " · ".join(part for part in (timeline.provider, timeline.model) if part) or "no model"
                    )
                    theme.pill(model_text, icon_name="cpu", tone="muted").tooltip(model_text)
                    if timeline.cwd:
                        theme.pill(
                            theme.truncate(timeline.cwd, 44), icon_name="folder", tone="muted"
                        ).tooltip(timeline.cwd)
            with ui.element("div").classes("dp-head-side"):
                theme.badge(status_text, tone=status_tone)
                if timeline.pending:
                    theme.badge(f"{timeline.pending} QUEUED", tone="warn")
            with ui.element("div").classes("dp-head-actions"):
                if self._session_id and not timeline.active and not timeline.error:
                    ui.button("Resume", on_click=self._resume_session).props(
                        "unelevated dense no-caps"
                    ).classes("dp-primary-btn").style(theme.PRIMARY_BUTTON_STYLE)
                if timeline.busy and self._session_id:
                    ui.button(
                        "Interrupt",
                        icon=theme.ICONS["stop"],
                        on_click=self._interrupt_turn,
                    ).props("flat dense no-caps").classes("dp-ghost-btn")
                ui.button(icon=theme.ICONS["settings"], on_click=self._open_settings).props(
                    "flat round dense"
                ).classes("dp-icon-btn").tooltip("Settings")
                ui.button(icon=theme.ICONS["more"], on_click=self._open_palette).props(
                    "flat round dense"
                ).classes("dp-icon-btn").tooltip("Command palette")

    def _paint_messages(self) -> None:
        message_list = self._message_list
        if message_list is None:
            return
        message_list.clear()
        with message_list:
            if self._notice is not None:
                self._paint_notice(*self._notice)
            if self._stream_error:
                self._paint_stream_error()
            timeline = self._timeline
            if timeline.messages:
                joined = {
                    call.call_id
                    for message in timeline.messages
                    if message.role != "tool"
                    for call in message.tool_calls
                    if call.call_id
                }
                for message in timeline.messages:
                    self._paint_message(message, joined)
            elif timeline.error:
                self._paint_empty(
                    icon_name="warning",
                    title="Session unavailable",
                    note=timeline.error,
                    retry=True,
                )
            elif self._session_id is None:
                self._paint_empty(
                    icon_name="forum",
                    title="No session yet",
                    note="Start a new session to talk to DeepPyro.",
                    new_session=True,
                )
            else:
                self._paint_empty(
                    icon_name="spark",
                    title="No messages yet",
                    note="Send the first message below, or type /help for commands.",
                )

    def _paint_empty(
        self,
        *,
        icon_name: str,
        title: str,
        note: str,
        retry: bool = False,
        new_session: bool = False,
    ) -> None:
        with ui.element("div").classes("dp-empty-card dp-chat-empty"):
            theme.icon(icon_name, size="22px", colour="var(--dp-muted)")
            ui.label(title).classes("dp-empty-title")
            ui.label(note).classes("dp-empty-note")
            if retry or new_session:
                with ui.element("div").classes("dp-empty-actions"):
                    if retry:
                        ui.button("Retry", on_click=self._retry).props("flat dense no-caps").classes(
                            "dp-link-btn"
                        )
                    if new_session:
                        ui.button("New session", on_click=self._create_session).props(
                            "unelevated dense no-caps"
                        ).classes("dp-primary-btn").style(theme.PRIMARY_BUTTON_STYLE)

    def _paint_notice(self, status: str, message: str) -> None:
        tone = "ok" if status == STATUS_OK else "warn" if status == STATUS_REDIRECT else "danger"
        icon_name = "check" if tone == "ok" else "info" if tone == "warn" else "warning"
        with ui.element("div").classes(f"dp-notice dp-notice-{tone}"):
            theme.icon(icon_name, size="14px")
            ui.label(message).classes("dp-notice-text")

    def _paint_stream_error(self) -> None:
        with ui.element("div").classes("dp-notice dp-notice-error"):
            theme.icon("warning", size="14px", colour="var(--dp-danger)")
            ui.label(f"Live updates stopped: {self._stream_error}").classes("dp-notice-text")
            ui.button("Reconnect", on_click=self._restart_stream).props("flat dense no-caps").classes(
                "dp-link-btn"
            )

    def _paint_message(self, message: MessageState, joined: set[str]) -> None:
        if message.role == "user":
            self._paint_user_message(message)
        elif message.role == "tool":
            # The producer normally joins tool results into their assistant call and drops
            # the synthetic row; skip it defensively when both shapes arrive.
            if message.tool_call_id and message.tool_call_id in joined:
                return
            self._paint_tool_message(message)
        else:
            self._paint_assistant_message(message)

    def _paint_brand_avatar(self) -> None:
        with ui.element("div").classes("dp-avatar dp-message-avatar dp-avatar-brand"):
            ui.image("/brand/cyborg-mark.svg").classes("dp-avatar-img")

    def _paint_user_message(self, message: MessageState) -> None:
        with ui.element("div").classes("dp-message dp-message-user"):
            theme.avatar(USER_NAME, colour=USER_COLOUR, size=34).classes("dp-message-avatar")
            with ui.element("div").classes("dp-message-main"):
                with ui.element("div").classes("dp-message-head"):
                    ui.label(USER_NAME).classes("dp-message-name")
                    ui.label(theme.clock_time(message.created_at)).classes("dp-message-time")
                with ui.element("div").classes("dp-message-body"):
                    if message.text:
                        ui.label(message.text).classes("dp-message-text")
                    if message.images:
                        theme.badge(f"{len(message.images)} IMAGE", tone="muted").tooltip(
                            "\n".join(message.images)
                        )

    def _paint_assistant_message(self, message: MessageState) -> None:
        badge = source_badge(message.source_kind)
        if not (message.text or message.reasoning or message.tool_calls or message.is_error or badge):
            return
        with ui.element("div").classes("dp-message dp-message-assistant"):
            self._paint_brand_avatar()
            with ui.element("div").classes("dp-message-main"):
                with ui.element("div").classes("dp-message-head"):
                    ui.label(ASSISTANT_NAME).classes("dp-message-name")
                    if badge is not None:
                        theme.badge(badge[0], tone=badge[1])
                    if message.is_error:
                        theme.badge("ERROR", tone="danger")
                    ui.label(theme.clock_time(message.created_at)).classes("dp-message-time")
                with ui.element("div").classes("dp-message-body"):
                    if message.reasoning:
                        with ui.expansion("Reasoning", icon=theme.ICONS["spark"]).classes("dp-reasoning"):
                            ui.markdown(message.reasoning).classes("dp-markdown dp-reasoning-body")
                    if message.text:
                        ui.markdown(message.text).classes("dp-markdown")
                    if message.images:
                        theme.badge(f"{len(message.images)} IMAGE", tone="muted").tooltip(
                            "\n".join(message.images)
                        )
                    for call in message.tool_calls:
                        self._paint_tool_card(call)

    def _paint_tool_message(self, message: MessageState) -> None:
        call = ToolCallState(
            call_id=message.tool_call_id or message.message_id,
            name="tool result",
            result=message.text,
            is_error=message.is_error,
            pending=not message.text,
        )
        with ui.element("div").classes("dp-message dp-message-tool"):
            with ui.element("div").classes("dp-message-tool-icon"):
                theme.icon("terminal", size="15px")
            with ui.element("div").classes("dp-message-main"):
                with ui.element("div").classes("dp-message-head"):
                    ui.label("Tool result").classes("dp-message-name")
                    ui.label(theme.clock_time(message.created_at)).classes("dp-message-time")
                self._paint_tool_card(call)

    def _paint_tool_card(self, call: ToolCallState) -> None:
        status = call.status
        with ui.element("div").classes("dp-tool"):
            with ui.element("div").classes("dp-tool-head"):
                theme.icon("terminal", size="13px", colour="var(--dp-muted)")
                ui.label(call.name).classes("dp-tool-name")
                if call.call_id:
                    ui.label(theme.short_id(call.call_id)).classes("dp-tool-id")
                theme.badge(status.value.upper(), tone=tool_status_tone(status))
            with ui.element("div").classes("dp-tool-body"):
                if call.arguments:
                    with ui.element("div").classes("dp-tool-section"):
                        ui.label("ARGUMENTS").classes("dp-tool-label")
                        with ui.element("pre").classes("dp-tool-pre"):
                            ui.label(call.arguments).classes("dp-tool-pre-text")
                if status is ToolStatus.PENDING:
                    with ui.element("div").classes("dp-tool-section"):
                        ui.label("Running…").classes("dp-tool-pending")
                elif call.result is not None:
                    with ui.element("div").classes("dp-tool-section"):
                        ui.label("RESULT").classes("dp-tool-label")
                        with ui.element("pre").classes("dp-tool-pre"):
                            ui.label(call.result).classes("dp-tool-pre-text")

    def _paint_approvals(self) -> None:
        slot = self._approvals_slot
        if slot is None:
            return
        approvals = self._approvals
        slot.clear()
        with slot:
            for approval in approvals:
                self._paint_approval(approval)
        if approvals:
            slot.set_visibility(True)
        else:
            slot.set_visibility(False)

    def _paint_approval(self, approval: ApprovalState) -> None:
        with ui.element("div").classes("dp-approval"):
            with ui.element("div").classes("dp-approval-head"):
                theme.icon("shield", size="15px", colour="var(--dp-warn)")
                ui.label(f"Approval required · {approval.tool}").classes("dp-approval-title")
                theme.badge("PENDING", tone="warn")
            if approval.summary:
                ui.label(approval.summary).classes("dp-approval-summary")
            if approval.detail:
                ui.label(approval.detail).classes("dp-approval-detail")
            with ui.element("div").classes("dp-approval-actions"):
                ui.button(
                    "Allow once", on_click=lambda: self._decide_approval(approval.approval_id, "allow")
                ).props("unelevated dense no-caps").classes("dp-approval-allow").style(
                    theme.PRIMARY_BUTTON_STYLE
                )
                ui.button("Deny", on_click=lambda: self._decide_approval(approval.approval_id, "deny")).props(
                    "flat dense no-caps"
                ).classes("dp-ghost-btn dp-approval-deny")

    def _is_read_only(self) -> bool:
        return self._session_id is not None and not self._timeline.active

    def _can_submit(self) -> bool:
        return self._session_id is not None and not self._is_read_only()

    def _paint_composer(self) -> None:
        if self._input is None or self._send_btn is None or self._hint is None:
            return
        locked = not self._can_submit() or self._sending
        if locked:
            self._input.disable()
            self._send_btn.disable()
        else:
            self._input.enable()
            self._send_btn.enable()
        timeline = self._timeline
        if self._session_id is None:
            hint = "No session selected — use New session above."
        elif self._is_read_only():
            hint = "Read-only session — Resume to send messages."
        elif timeline.busy:
            hint = "DeepPyro is working — Interrupt to stop."
        else:
            hint = "Enter sends · Shift+Enter adds a line · / for commands"
        self._hint.set_text(hint)
        if self._composer_status is not None:
            status = f"{timeline.pending} queued" if timeline.pending else ""
            self._composer_status.set_text(status)

    # -- draft rendering --------------------------------------------------------

    def _paint_draft(self) -> None:
        row = self._draft_row
        if row is None or self._stopped:
            return
        draft = self._draft
        if not draft.has_content:
            row.set_visibility(False)
            return
        row.set_visibility(True)
        if self._draft_status is not None:
            self._draft_status.set_text("FINALISING" if draft.settle_seq is not None else "STREAMING")
        if self._draft_reasoning is not None:
            if draft.reasoning:
                self._draft_reasoning.set_visibility(True)
                if self._draft_reasoning_body is not None:
                    self._draft_reasoning_body.set_text(draft.reasoning)
            else:
                self._draft_reasoning.set_visibility(False)
        if self._draft_text is not None:
            cursor = "" if draft.settle_seq is not None else "▍"
            self._draft_text.set_text(f"{draft.text}{cursor}" if draft.text else "…")

    def _flush_draft(self, *, force: bool = False) -> None:
        if self._stopped:
            return
        now = time.monotonic()
        if not force and now - self._draft_painted_at < DRAFT_PAINT_INTERVAL:
            return
        self._draft_painted_at = now
        try:
            self._paint_draft()
        except RuntimeError:
            self._stopped = True
            return
        self._scroll_to_bottom()

    # -- scrolling --------------------------------------------------------------

    def _on_scroll(self, event: Any) -> None:
        percentage = getattr(event, "vertical_percentage", None)
        if percentage is None:
            return
        try:
            self._stick_to_bottom = float(percentage) >= SCROLL_STICK_THRESHOLD
        except (TypeError, ValueError):
            return

    def _scroll_to_bottom(self, *, force: bool = False) -> None:
        if self._messages_area is None or self._stopped:
            return
        if not force and not self._stick_to_bottom:
            return
        now = time.monotonic()
        if not force and now - self._scroll_at < SCROLL_INTERVAL:
            return
        self._scroll_at = now
        with contextlib.suppress(RuntimeError):
            self._messages_area.scroll_to(percent=1.0)

    # -- composer actions -------------------------------------------------------

    def _composer_text(self) -> str:
        if self._input is None:
            return ""
        value = self._input.value
        return value if isinstance(value, str) else ""

    def _set_composer_text(self, text: str) -> None:
        if self._input is None:
            return
        with contextlib.suppress(RuntimeError):
            self._input.set_value(text)

    async def _send_current(self) -> None:
        if self._stopped or self._sending:
            return
        text = self._composer_text().strip()
        if not text:
            return
        if self._session_id is None:
            theme.notify_error("No session is selected.")
            return
        if self._is_read_only():
            theme.notify_error("Resume the session before sending a message.")
            return
        self._sending = True
        self._paint_composer()
        try:
            if text.startswith("/"):
                await self._run_command(text)
            else:
                self._notice = None
                await self._bridge.send(self._session_id, text)
                self._set_composer_text("")
                self._signature = None
                await self._refresh(force=True)
        except Exception as exc:  # noqa: BLE001 - user-visible failure
            theme.notify_error(bridge_error(exc))
        finally:
            self._sending = False
            self._paint_composer()

    async def _run_command(self, text: str) -> None:
        parsed = split_slash_command(text)
        if parsed is None:
            self._notice = (STATUS_ERROR, "Commands start with a slash, for example /help.")
            await self._refresh(force=True)
            return
        name, args = parsed
        result = await self._bridge.execute(name, args or None, self._session_id)
        status = result.status
        if status == STATUS_REDIRECT and result.redirect:
            self._notice = (STATUS_OK, result.message or f"Opening {result.redirect}")
            self._set_composer_text("")
            self._navigate(result.redirect)
            return
        session_id = result.data.get("session_id") if isinstance(result.data, Mapping) else None
        if status == STATUS_OK and session_id:
            self._notice = (STATUS_OK, result.message or "Switched session.")
            self._set_composer_text("")
            self._navigate_session(str(session_id))
            return
        self._notice = (status, result.message or f"{name} → {status}")
        if status == STATUS_OK:
            self._set_composer_text("")
        elif status == STATUS_ERROR:
            theme.notify_error(result.message or f"{name} failed.")
        await self._refresh(force=True)

    async def _interrupt_turn(self) -> None:
        if self._session_id is None:
            return
        try:
            result = await self._bridge.interrupt(self._session_id)
        except Exception as exc:  # noqa: BLE001 - user-visible failure
            theme.notify_error(bridge_error(exc))
            return
        detail = _as_text(result.get("detail")) if isinstance(result, Mapping) else ""
        self._notice = (STATUS_OK, detail or "Interrupt requested.")
        self._signature = None
        await self._refresh(force=True)

    async def _resume_session(self) -> None:
        if self._session_id is None:
            return
        try:
            await self._bridge.resume(self._session_id)
        except Exception as exc:  # noqa: BLE001 - user-visible failure
            theme.notify_error(bridge_error(exc))
            return
        self._navigate_session(self._session_id)

    async def _create_session(self) -> None:
        if self._creating:
            return
        self._creating = True
        try:
            result = await self._bridge.create()
            session_id = _as_text(result.get("session_id")) if isinstance(result, Mapping) else ""
            if not session_id:
                theme.notify_error("Session creation returned no session id.")
                return
            self._navigate_session(session_id)
        except Exception as exc:  # noqa: BLE001 - user-visible failure
            theme.notify_error(bridge_error(exc))
        finally:
            self._creating = False

    async def _retry(self) -> None:
        self._stream_error = ""
        self._signature = None
        await self._refresh(force=True)
        if self._session_id and (self._stream_task is None or self._stream_task.done()):
            self._start_stream()

    async def _restart_stream(self) -> None:
        self._stream_error = ""
        self._signature = None
        await self._refresh(force=True)
        self._start_stream()

    async def _decide_approval(self, approval_id: str, decision: str) -> None:
        if self._deciding:
            return
        self._deciding = True
        try:
            settled = self._bridge.decide_approval(approval_id, decision)
            if not settled:
                theme.notify_error("That approval was already settled.")
        except Exception as exc:  # noqa: BLE001 - user-visible failure
            theme.notify_error(bridge_error(exc))
        finally:
            self._deciding = False
        self._signature = None
        await self._refresh(force=True)
