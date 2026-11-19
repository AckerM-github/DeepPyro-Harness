"""Shared presentation helpers for the NiceGUI web UI.

The pages are Python objects, not a template language. These helpers keep the
small repeated decisions (Material icon names, initials, relative stamps and
the stylesheet hand-off) in one place so chat, settings and palette agree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from html import escape
from typing import Any

from nicegui import ui

#: The four stylesheets the shell loads; each has one owner.
STYLESHEETS = ("/brand/style.css", "/brand/chat.css", "/brand/settings.css")

#: Quasar paints every ui.button with bg-primary; an inline important style is
#: the only reliable way to repaint the primary action in the shell accent.
PRIMARY_BUTTON_STYLE = "background: var(--dp-accent) !important; color: var(--dp-accent-ink) !important"

#: Material icon names used by the shell. Keeping the map explicit makes a
#: missing glyph a code review issue rather than a runtime surprise.
ICONS: dict[str, str] = {
    "sessions": "forum",
    "commands": "terminal",
    "plugins": "extension",
    "search": "search",
    "settings": "settings",
    "add": "add",
    "help": "help_outline",
    "more": "more_horiz",
    "send": "arrow_upward",
    "stop": "stop",
    "attach": "attach_file",
    "close": "close",
    "check": "check",
    "chevron": "expand_more",
    "folder": "folder_open",
    "bolt": "bolt",
    "cpu": "memory",
    "clock": "schedule",
    "team": "group",
    "goal": "flag",
    "archive": "archive",
    "refresh": "refresh",
    "external": "open_in_new",
    "shield": "shield",
    "key": "key",
    "globe": "public",
    "monitor": "computer",
    "info": "info",
    "warning": "warning",
    "copy": "content_copy",
    "terminal": "terminal",
    "spark": "auto_awesome",
}

#: Stable accent avatars for sessions without a custom colour.
_AVATAR_COLOURS = (
    "#1f6feb",
    "#2ea043",
    "#a371f7",
    "#db6d28",
    "#e5534b",
    "#1f9e9e",
    "#bf8700",
)


def install_head() -> None:
    """Put the local stylesheets and the icon font on the current page."""
    for href in STYLESHEETS:
        ui.add_head_html(f'<link rel="stylesheet" href="{href}">')


def icon(name: str, *, size: str = "16px", colour: str = "currentColor") -> ui.icon:
    """One Material icon with the shell's default sizing."""
    return ui.icon(ICONS.get(name, name)).style(f"font-size:{size};color:{colour}")


def initials(name: str | None) -> str:
    """Two-character initials for a person, session or plugin name."""
    words = [word for word in str(name or "?").replace("_", " ").replace("-", " ").split() if word]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def avatar_colour(seed: str | None) -> str:
    """Deterministic colour for an avatar."""
    text = str(seed or "")
    return _AVATAR_COLOURS[sum(ord(character) for character in text) % len(_AVATAR_COLOURS)]


def avatar(name: str | None, *, colour: str | None = None, size: int = 34) -> ui.element:
    """A round initials avatar; NiceGUI's avatar icon API cannot show text."""
    element = (
        ui.element("div")
        .classes("dp-avatar")
        .style(f"width:{size}px;height:{size}px;background:{colour or avatar_colour(name)}")
    )
    with element:
        ui.label(initials(name)).classes("dp-avatar-text")
    return element


def badge(text: str, *, tone: str = "muted") -> ui.label:
    """A compact, non-interactive status pill."""
    return ui.label(text).classes(f"dp-badge dp-badge-{tone}")


def pill(text: str, *, icon_name: str | None = None, tone: str = "muted") -> ui.element:
    """A badge with an optional leading icon."""
    element = ui.element("div").classes(f"dp-pill dp-pill-{tone}")
    with element:
        if icon_name:
            icon(icon_name, size="13px")
        ui.label(text).classes("dp-pill-text")
    return element


def human_time(value: Any) -> str:
    """Render an ISO timestamp as a short relative or clock stamp."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    seconds = max(int((now - moment).total_seconds()), 0)
    if seconds < 45:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 7 * 86400:
        return f"{seconds // 86400}d ago"
    return moment.strftime("%b %d")


def clock_time(value: Any) -> str:
    """Render a timestamp as a local clock time for a message header."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(float(value) / 1000)
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return str(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%H:%M")


def short_id(value: str | None, *, length: int = 8) -> str:
    text = str(value or "")
    if text.startswith("session-"):
        text = text[8:]
    return text[:length] or "-"


def safe_text(value: Any) -> str:
    """Escape a value destined for a place where NiceGUI does not escape."""
    return escape("" if value is None else str(value))


def truncate(value: Any, limit: int = 80) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def notify_error(message: str) -> None:
    ui.notify(message, type="negative", position="bottom-right", timeout=6000)


def notify_ok(message: str) -> None:
    ui.notify(message, type="positive", position="bottom-right", timeout=4000)


__all__ = [
    "ICONS",
    "STYLESHEETS",
    "avatar",
    "avatar_colour",
    "badge",
    "clock_time",
    "human_time",
    "icon",
    "initials",
    "install_head",
    "notify_error",
    "notify_ok",
    "pill",
    "safe_text",
    "short_id",
    "truncate",
]
