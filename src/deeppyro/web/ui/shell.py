"""Static single-page shell for the DeepPyro local web surface.

The shell is deliberately data-free: it ships the four-region frame, the
overlay containers, and links to external same-origin assets. It never embeds
live state, never mocks a session, and never reads the agent runtime. The
browser fetches everything from the same-origin JSON API and renders it with
DOM text nodes (see assets/app.js).

Hosts inject the body fragment returned by build_shell_body into an HTML page.
build_shell wraps the same markup in a complete document for static tests and
plain-HTML fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from html import escape
from urllib.parse import unquote

DEFAULT_SETTINGS_SECTION = "provider"
SHELL_TITLE = "DeepPyro"

ASSET_CSS = "/ui/app.css"
ASSET_JS = "/ui/app.js"
ASSET_MARK = "/ui/cyborg-mark.svg"
ASSET_FONT = "/ui/InterVariable.woff2"

ICONS: dict[str, str] = {
    "hash": '<path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "more": '<circle cx="5" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="19" cy="12" r="1" fill="currentColor"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "settings": '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.38a2 2 0 0 0-.73-2.73l-.15-.1a2 2 0 0 1-1-1.72v-.51a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/>',
    "command": '<path d="M18 3a3 3 0 0 0-3 3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12a3 3 0 0 0 3-3z"/>',
    "message": '<path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z"/>',
    "layers": '<path d="m12 2 9 5-9 5-9-5 9-5z"/><path d="m3 12 9 5 9-5M3 17l9 5 9-5"/>',
    "refresh": '<path d="M20 11a8 8 0 0 0-14.9-4M4 4v5h5M4 13a8 8 0 0 0 14.9 4M20 20v-5h-5"/>',
    "cpu": '<rect x="6" y="6" width="12" height="12" rx="2"/><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
    "monitor": '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "external": '<path d="M15 3h6v6M10 14 21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>',
    "arrow-up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
}

RAIL_HTML = """
<aside class="rail" id="rail" aria-label="Primary">
  <a class="rail-logo" href="/" title="DeepPyro">{logo}</a>
  <div class="rail-sep"></div>
  <a class="rail-ws active" href="/" title="DeepPyro workspace"><span class="rail-pill"></span>DP</a>
  <button class="rail-add" type="button" id="rail-new-session" title="New session">{plus}</button>
  <div class="rail-foot">
    <a class="rail-mini" href="/palette" title="Command palette">{command}</a>
    <a class="rail-mini" href="/settings" title="Settings">{settings}</a>
    <a class="rail-avatar" href="/settings/about" title="Local workspace">DP<span class="dot"></span></a>
  </div>
</aside>
"""

SIDEBAR_HTML = """
<aside class="sidebar" id="sidebar" aria-label="Sessions">
  <div class="workspace-head">
    <div>
      <div class="workspace-kicker">WORKSPACE</div>
      <div class="workspace-name"><span id="workspace-name">DeepPyro</span><span class="chev">{chevron_down}</span></div>
      <div class="workspace-path" id="workspace-path" title="Session working directory">—</div>
    </div>
    <button class="icon-btn top-icon" type="button" id="session-list-refresh" title="Refresh sessions">{refresh}</button>
  </div>
  <div class="sidebar-scroll">
    <a class="search-box" href="/palette"><span>{search}</span><span>Find or start a session</span>{kbd}</a>
    <a class="nav-item active" href="/" id="nav-sessions">{message_icon}<span>Sessions</span><span class="nav-count" id="nav-count-sessions">—</span></a>
    <a class="nav-item" href="/palette">{command_icon}<span>Commands</span><span class="nav-count" id="nav-count-commands">—</span></a>
    <a class="nav-item" href="/settings/plugins">{layers_icon}<span>Plugins</span><span class="nav-count" id="nav-count-plugins">—</span></a>
    <div class="section-head">Sessions <button class="plus" type="button" id="new-session" title="New session">{plus_small}</button></div>
    <div class="session-list" id="session-list" aria-live="polite">
      <div class="state-block compact"><span class="spinner"></span><div><b>Loading sessions</b><p>Reading the local API.</p></div></div>
    </div>
    <div class="section-head">Runtime</div>
    <div class="sidebar-runtime">
      <div class="live-row"><span class="live-dot" id="sidebar-live-dot"></span><span>Event stream</span><span class="live-time" id="sidebar-stream-state">connecting</span></div>
      <div class="live-row">{cpu_icon}<span id="sidebar-model">—</span><span class="live-time">model</span></div>
      <div class="live-row">{monitor_icon}<span id="sidebar-web">—</span><span class="live-time">web</span></div>
    </div>
  </div>
  <div class="sidebar-foot">
    <div class="foot-avatar">DP<span class="dot"></span></div>
    <div class="foot-meta"><div class="foot-name" id="foot-name">DeepPyro</div><div class="foot-status" id="foot-status">local runtime</div></div>
    <div class="foot-actions">
      <a class="icon-btn" href="/settings/web" title="Web host">{monitor_icon}</a>
      <a class="icon-btn" href="/settings" title="Settings">{settings_icon}</a>
    </div>
  </div>
</aside>
"""

STAGE_HTML = """
<main class="stage" id="stage" aria-label="Conversation">
  <header class="channelbar">
    <div class="channel-title"><span class="hash-big">#</span><h1 id="session-title">Session</h1><span class="channel-topic" id="session-topic">Live view of the selected local session.</span></div>
    <div class="channel-actions">
      <button class="top-icon" type="button" id="open-palette" title="Command palette">{command_icon}</button>
      <button class="top-icon" type="button" id="refresh-session" title="Reload transcript">{refresh_icon}</button>
      <div class="top-divider"></div>
      <a class="top-icon" href="/settings" title="Settings">{settings_icon}</a>
    </div>
  </header>
  <div class="runbar">
    <span class="run-status" id="run-status">● idle</span><span class="run-id" id="run-id">SESSION —</span>
    <span id="run-provider">—</span><span class="run-spacer"></span>
    <span class="run-model" id="run-model">—</span><span class="run-meter"><i id="run-meter"></i></span><span class="run-time" id="run-step">—</span>
  </div>
  <div class="chat-scroll" id="chat-scroll">
    <div class="chat-inner" id="message-list" aria-live="polite">
      <div class="state-block"><span class="spinner"></span><div><b>Loading transcript</b><p>Waiting for the local API.</p></div></div>
    </div>
  </div>
  <div class="composer-shell">
    <div class="composer-queue" id="composer-queue" aria-live="polite"></div>
    <div class="composer">
      <button class="composer-plus" type="button" id="composer-commands" title="Commands">{plus_icon}</button>
      <textarea class="composer-field" id="composer-input" rows="1" spellcheck="false" placeholder="Message the session" aria-label="Message the session"></textarea>
      <div class="composer-tools">
        <span class="queue-badge is-hidden" id="composer-queue-count" title="Queued messages"></span>
        <button class="composer-tool is-hidden" type="button" id="composer-stop" title="Stop the current turn">{stop_icon}</button>
        <button class="composer-send" type="button" id="composer-send" title="Send">{arrow_up_icon}</button>
      </div>
    </div>
    <div class="composer-hint"><span class="hint-left"><span><b>/</b> commands</span><span><b>Shift</b> + <b>Enter</b> newline</span><span id="composer-queue-hint"></span></span><span class="ready" id="connection-state">● connecting</span></div>
  </div>
</main>
"""

INSPECTOR_HTML = """
<aside class="inspector" id="inspector" aria-label="Session inspector">
  <div class="inspector-head"><span>Session</span><button class="icon-btn" type="button" id="inspector-refresh" title="Refresh inspector">{refresh_small}</button></div>
  <div class="session-card">
    <div class="session-top">
      <div class="session-icon">{logo_small}</div>
      <div><div class="session-name" id="inspector-title">No session</div><div class="session-id" id="inspector-id">—</div></div>
      <span class="live-chip is-hidden" id="inspector-live"><i></i>LIVE</span>
    </div>
    <div class="session-metrics">
      <div class="metric"><div class="metric-label">Model</div><div class="metric-value" id="metric-model">—</div></div>
      <div class="metric"><div class="metric-label">Provider</div><div class="metric-value" id="metric-provider">—</div></div>
      <div class="metric"><div class="metric-label">Preset</div><div class="metric-value" id="metric-preset">—</div></div>
      <div class="metric"><div class="metric-label">Turns</div><div class="metric-value" id="metric-turns">—</div></div>
    </div>
    <div class="context-block">
      <div class="context-line"><span>CONTEXT</span><span><b id="context-tokens">—</b> / <span id="context-window">—</span></span></div>
      <div class="context-track"><i id="context-track"></i></div>
    </div>
    <div class="detail-list">
      <div class="detail-row"><span class="detail-label">Permission</span><span class="detail-value" id="permission-mode">—</span></div>
      <div class="detail-row"><span class="detail-label">Approval</span><span class="detail-value" id="permission-approval">—</span></div>
      <div class="detail-row"><span class="detail-label">Working dir</span><span class="detail-value detail-mono" id="session-cwd">—</span></div>
    </div>
    <a class="session-link" id="transcript-link" href="#" target="_blank" rel="noreferrer"><span>Open raw transcript</span>{external_small}</a>
  </div>
  <div class="panel-section">
    <div class="panel-section-head"><span>Usage</span><span class="panel-action">tokens</span></div>
    <div class="shortcuts">
      <div class="shortcut"><span>Input</span><span id="usage-input">—</span></div>
      <div class="shortcut"><span>Output</span><span id="usage-output">—</span></div>
      <div class="shortcut"><span>Cached input</span><span id="usage-cached">—</span></div>
    </div>
  </div>
  <div class="panel-section">
    <div class="panel-section-head"><span>Runtime</span><span class="panel-action">local</span></div>
    <div class="shortcuts">
      <div class="shortcut"><span>Sessions</span><span id="inspector-count-sessions">—</span></div>
      <div class="shortcut"><span>Commands</span><span id="inspector-count-commands">—</span></div>
      <div class="shortcut"><span>Plugins</span><span id="inspector-count-plugins">—</span></div>
      <div class="shortcut"><span>Web</span><span id="web-host">—</span></div>
      <div class="shortcut"><span>Single session</span><span id="web-single">—</span></div>
    </div>
  </div>
  <div class="panel-section">
    <div class="panel-section-head"><span>Shortcuts</span></div>
    <div class="shortcuts">
      <div class="shortcut"><span>Command palette</span>{kbd_cmd_k}</div>
      <div class="shortcut"><span>Settings</span>{kbd_cmd_comma}</div>
      <div class="shortcut"><span>New session</span>{kbd_cmd_n}</div>
    </div>
  </div>
</aside>
"""

PALETTE_HTML = """
<div class="overlay-root" id="palette-overlay" role="dialog" aria-modal="true" aria-labelledby="palette-title">
  <div class="palette">
    <div class="palette-search"><span class="slash-badge">/</span><input id="palette-search" type="search" autocomplete="off" spellcheck="false" placeholder="Search commands, sessions, settings" aria-label="Command palette search"><button class="esc-key" type="button" id="palette-close" title="Close palette">ESC</button></div>
    <h2 class="sr-only" id="palette-title">Command palette</h2>
    <div class="palette-body" id="palette-results"><div class="state-block compact"><span class="spinner"></span><div><b>Loading palette</b><p>Reading commands from the local API.</p></div></div></div>
    <div class="palette-foot"><span><b>↑↓</b> navigate</span><span><b>↵</b> select</span><span><b>esc</b> close</span><span class="palette-brand">DEEPPYRO / LOCAL</span></div>
  </div>
</div>
"""

NEW_SESSION_HTML = """
<div class="overlay-root" id="new-session-overlay" role="dialog" aria-modal="true" aria-labelledby="new-session-title">
  <div class="new-session-modal">
    <header class="new-session-head">
      <div><h2 id="new-session-title">New session</h2><p>Each session keeps the working directory it starts in. The current session stays in the sidebar.</p></div>
      <button class="icon-btn top-icon" type="button" id="new-session-close" aria-label="Close new session dialog">{x_icon}</button>
    </header>
    <form id="new-session-form">
      <div class="new-session-body">
        <label class="field" for="new-session-cwd">
          <span class="field-label">Working directory <span class="field-hint">absolute path</span></span>
          <span class="input-shell">{folder_icon}<input id="new-session-cwd" type="text" autocomplete="off" spellcheck="false" placeholder="D:/path/to/project"></span>
          <span class="field-note" id="new-session-default">Leave empty to use the host default workspace.</span>
        </label>
        <div class="recent-block">
          <div class="field-label">Recent workspaces</div>
          <div class="recent-paths" id="new-session-recent"><span class="field-note">No recent workspaces yet.</span></div>
        </div>
        <p class="new-session-error" id="new-session-error" role="alert" hidden></p>
      </div>
      <footer class="new-session-foot">
        <button class="ghost-btn" type="button" id="new-session-cancel">Cancel</button>
        <button class="primary-btn" type="submit" id="new-session-submit">Start session</button>
      </footer>
    </form>
  </div>
</div>
"""

SETTINGS_HTML = """
<div class="overlay-root" id="settings-overlay" role="dialog" aria-modal="true" aria-labelledby="settings-title">
  <div class="settings-modal">
    <aside class="settings-nav" aria-label="Settings sections">
      <div class="settings-brand">{logo_small}<div><div class="brand-name">DeepPyro</div><div class="brand-sub">settings · local</div></div></div>
      <div id="settings-nav-list"><div class="state-block compact"><span class="spinner"></span><div><b>Loading sections</b></div></div></div>
      <div class="settings-nav-foot" id="settings-nav-foot">local runtime</div>
    </aside>
    <main class="settings-content">
      <header class="settings-head"><div><h2 id="settings-title">Settings</h2><p id="settings-subtitle">Loading settings from the local API.</p></div><a class="settings-close" id="settings-close" href="/" aria-label="Close settings">{x_icon}</a></header>
      <div class="settings-body" id="settings-body"><div class="state-block"><span class="spinner"></span><div><b>Loading settings</b><p>Fetching configuration from the local API.</p></div></div></div>
      <footer class="settings-footer" id="settings-footer"><span class="footer-note" id="settings-footer-note">Local API · same origin</span><span class="settings-footer-actions" id="settings-footer-actions"></span></footer>
    </main>
  </div>
</div>
"""


def icon(name: str, size: int = 17, cls: str = "") -> str:
    """Render one inline, trusted SVG icon from the static table."""

    body = ICONS.get(name, ICONS["plus"])
    classes = f"icon {cls}".strip()
    return (
        f'<svg class="{classes}" width="{size}" height="{size}" viewBox="0 0 24 24" '
        'fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


def brand(size: int = 40, cls: str = "") -> str:
    """Render the local brand mark from the same-origin asset bundle."""

    return f'<img class="{cls}" src="{ASSET_MARK}" width="{size}" height="{size}" alt="" draggable="false">'


def kbd(text: str) -> str:
    """Render a keyboard hint label."""

    return f'<span class="kbd">{escape(text)}</span>'


class ShellView(StrEnum):
    """Which surface the browser should present first."""

    CHAT = "chat"
    PALETTE = "palette"
    SETTINGS = "settings"


@dataclass(frozen=True, slots=True)
class ShellState:
    """Resolved route state for one shell render."""

    path: str
    view: ShellView
    session_id: str | None = None
    settings_section: str | None = None

    @property
    def document_title(self) -> str:
        """Human-readable document title for the resolved route."""

        if self.view is ShellView.PALETTE:
            return f"Command palette · {SHELL_TITLE}"
        if self.view is ShellView.SETTINGS:
            section = self.settings_section or DEFAULT_SETTINGS_SECTION
            return f"{section} · Settings · {SHELL_TITLE}"
        return SHELL_TITLE


def _attr(value: object) -> str:
    return escape(str(value), quote=True)


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_segment(value: str | None) -> str | None:
    text = _nonempty(value)
    if text is None:
        return None
    return unquote(text.split("/", 1)[0]) or None


def _clean_path(path: str) -> str:
    candidate = str(path or "/").strip()
    for separator in ("?", "#"):
        candidate = candidate.split(separator, 1)[0]
    return candidate or "/"


def _session_from_path(path: str) -> str | None:
    prefix = "/c/"
    if not path.startswith(prefix):
        return None
    segment = path[len(prefix) :].split("/", 1)[0]
    return unquote(segment) or None


def _section_from_path(path: str) -> str | None:
    prefix = "/settings/"
    if not path.startswith(prefix):
        return None
    segment = path[len(prefix) :].split("/", 1)[0]
    return unquote(segment) or None


def render_route_state(
    *,
    path: str = "/",
    section: str | None = None,
    session_id: str | None = None,
) -> ShellState:
    """Resolve a request path into the shell state the browser should boot into.

    section and session_id are explicit route arguments from the host and win
    over anything parsed out of path. Parsed segments are URL-decoded and kept
    raw here; escaping happens at render time.
    """

    clean = _clean_path(path)
    if clean == "/palette" or clean.startswith("/palette/"):
        return ShellState(path=clean, view=ShellView.PALETTE)
    if clean == "/settings" or clean.startswith("/settings/"):
        chosen = _first_segment(section) or _section_from_path(clean) or DEFAULT_SETTINGS_SECTION
        return ShellState(path=clean, view=ShellView.SETTINGS, settings_section=chosen)
    explicit = _nonempty(session_id)
    return ShellState(
        path=clean,
        view=ShellView.CHAT,
        session_id=explicit or _session_from_path(clean),
    )


def _rail() -> str:
    return RAIL_HTML.format(
        logo=brand(46),
        plus=icon("plus", 18),
        command=icon("command", 17),
        settings=icon("settings", 17),
    )


def _sidebar() -> str:
    return SIDEBAR_HTML.format(
        chevron_down=icon("chevron-down", 14),
        refresh=icon("refresh", 17),
        search=icon("search", 14),
        kbd=kbd("⌘K"),
        message_icon=icon("message", 16),
        command_icon=icon("command", 16),
        layers_icon=icon("layers", 16),
        plus_small=icon("plus", 13),
        cpu_icon=icon("cpu", 14),
        monitor_icon=icon("monitor", 14),
        settings_icon=icon("settings", 14),
    )


def _stage() -> str:
    return STAGE_HTML.format(
        command_icon=icon("command", 17),
        refresh_icon=icon("refresh", 17),
        settings_icon=icon("settings", 17),
        plus_icon=icon("plus", 15),
        stop_icon=icon("stop", 14),
        arrow_up_icon=icon("arrow-up", 15),
    )


def _inspector() -> str:
    return INSPECTOR_HTML.format(
        refresh_small=icon("refresh", 14),
        logo_small=brand(30),
        external_small=icon("external", 12),
        kbd_cmd_k=kbd("⌘ K"),
        kbd_cmd_comma=kbd("⌘ ,"),
        kbd_cmd_n=kbd("⌘ N"),
    )


def _palette_overlay() -> str:
    return PALETTE_HTML


def _new_session_overlay() -> str:
    return NEW_SESSION_HTML.format(
        x_icon=icon("x", 16),
        folder_icon=icon("folder", 14),
    )


def _settings_overlay() -> str:
    return SETTINGS_HTML.format(
        logo_small=brand(27),
        x_icon=icon("x", 17),
    )


def _app_root(state: ShellState, version: str, markup: str) -> str:
    view = state.view.value
    return (
        '<div id="dpyro-app" class="dpyro-app" '
        f'data-view="{_attr(view)}" '
        f'data-session-id="{_attr(state.session_id or "")}" '
        f'data-settings-section="{_attr(state.settings_section or "")}" '
        f'data-path="{_attr(state.path)}" '
        f'data-version="{_attr(version)}">'
        f"{markup}"
        "</div>"
    )


def _shell_markup(state: ShellState) -> str:
    blurred = "" if state.view is ShellView.CHAT else " is-blurred"
    return (
        f'<div class="app{blurred}" id="dpyro-shell">'
        f"{_rail()}{_sidebar()}{_stage()}{_inspector()}"
        "</div>"
        f"{_palette_overlay()}"
        f"{_new_session_overlay()}"
        f"{_settings_overlay()}"
        '<div class="toast-root" id="toast-root" role="status" aria-live="polite"></div>'
        '<noscript><div class="noscript">DeepPyro needs JavaScript and the local API. '
        "Enable scripts for this origin and reload the page.</div></noscript>"
    )


def build_shell_body(
    *,
    path: str = "/",
    section: str | None = None,
    session_id: str | None = None,
    version: str = "",
) -> str:
    """Return the body fragment a host injects into its page.

    The fragment is self-contained: it links the stylesheet and script that the
    host serves at /ui/app.css and /ui/app.js, so it works both inside an
    injected body and when copied into a plain HTML page.
    """

    state = render_route_state(path=path, section=section, session_id=session_id)
    markup = _app_root(state, version, _shell_markup(state))
    return "\n".join(
        (
            f'<link rel="stylesheet" href="{ASSET_CSS}">',
            markup,
            f'<script src="{ASSET_JS}" defer></script>',
        )
    )


def build_shell(
    *,
    path: str = "/",
    section: str | None = None,
    session_id: str | None = None,
    version: str = "",
) -> str:
    """Return a complete HTML document for tests and plain-HTML fallbacks."""

    state = render_route_state(path=path, section=section, session_id=session_id)
    markup = _app_root(state, version, _shell_markup(state))
    title = _attr(state.document_title)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n'
        f'<meta name="dpyro-version" content="{_attr(version)}">\n'
        f"<title>{title}</title>\n"
        f'<link rel="icon" href="{ASSET_MARK}" type="image/svg+xml">\n'
        f'<link rel="preload" href="{ASSET_FONT}" as="font" type="font/woff2" crossorigin>\n'
        f'<link rel="stylesheet" href="{ASSET_CSS}">\n'
        "</head>\n"
        "<body>\n"
        f"{markup}\n"
        f'<script src="{ASSET_JS}" defer></script>\n'
        "</body>\n"
        "</html>\n"
    )


__all__ = [
    "ASSET_CSS",
    "ASSET_FONT",
    "ASSET_JS",
    "ASSET_MARK",
    "DEFAULT_SETTINGS_SECTION",
    "ShellState",
    "ShellView",
    "build_shell",
    "build_shell_body",
    "render_route_state",
]
