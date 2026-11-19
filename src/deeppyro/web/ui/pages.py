"""NiceGUI page registration.

NiceGUI keeps page routes in a process-global registry, so registration happens
once and every page reads the currently configured bridge. Creating a second UI
server in the same process is not supported (see docs/design/nicegui-web-ui.md).
"""

from __future__ import annotations

from nicegui import ui

from deeppyro.web.ui import theme
from deeppyro.web.ui.bridge import WebUiBridge
from deeppyro.web.ui.layout import ShellView

_BRIDGE: WebUiBridge | None = None
_REGISTERED = False


def register_pages(bridge: WebUiBridge) -> None:
    """Bind the page callbacks to one bridge (idempotent per process)."""
    global _BRIDGE, _REGISTERED
    _BRIDGE = bridge
    if _REGISTERED:
        return
    _REGISTERED = True

    @ui.page("/", title="DeepPyro", favicon="/brand/cyborg-mark.svg", dark=True)
    async def index() -> None:
        await _render_shell(None, open_palette=False)

    @ui.page("/c/{session_id}", title="DeepPyro", favicon="/brand/cyborg-mark.svg", dark=True)
    async def session_page(session_id: str) -> None:
        await _render_shell(session_id, open_palette=False)

    @ui.page("/palette", title="DeepPyro", favicon="/brand/cyborg-mark.svg", dark=True)
    async def palette_page() -> None:
        await _render_shell(None, open_palette=True)

    @ui.page("/settings", title="DeepPyro settings", favicon="/brand/cyborg-mark.svg", dark=True)
    async def settings_page() -> None:
        await _render_settings("provider")

    @ui.page(
        "/settings/{section}",
        title="DeepPyro settings",
        favicon="/brand/cyborg-mark.svg",
        dark=True,
    )
    async def settings_section_page(section: str) -> None:
        await _render_settings(section)


async def _render_shell(session_id: str | None, *, open_palette: bool) -> None:
    bridge = _require_bridge()
    root = ui.element("div").classes("dp-page")
    await ShellView(
        bridge,
        session_id=session_id,
        open_palette=open_palette,
    ).mount(root)


async def _render_settings(section: str) -> None:
    from deeppyro.web.ui.settings_view import SettingsView

    bridge = _require_bridge()
    theme.install_head()
    root = ui.element("div").classes("dp-page dp-page-settings")
    await SettingsView(
        bridge,
        section=section,
        on_close=lambda: ui.navigate.to("/"),
    ).mount(root)


def _require_bridge() -> WebUiBridge:
    if _BRIDGE is None:  # pragma: no cover - server always registers first
        raise RuntimeError("NiceGUI pages were not registered with a bridge")
    return _BRIDGE


__all__ = ["register_pages"]
