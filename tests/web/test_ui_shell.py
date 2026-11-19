"""Guards for the live loopback web shell and its packaged UI assets.

These tests are static and in-process: they build the single-page shell with the
real render helpers and scan the shipped CSS, JS, SVG and font. They need no
socket, no browser and no network, so they stay fast and deterministic. They pin
the contract the server, the browser and the CSP all rely on: one SPA shell for
every route, four stable regions, live API endpoints only, escaped dynamic text,
packaged local assets, and no trace of the discarded static prototype.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from deeppyro.web.ui.shell import (
    ASSET_CSS,
    ASSET_FONT,
    ASSET_JS,
    ASSET_MARK,
    DEFAULT_SETTINGS_SECTION,
    ShellState,
    ShellView,
    build_shell,
    build_shell_body,
    render_route_state,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
UI_DIR = REPO_ROOT / "src" / "deeppyro" / "web" / "ui"
ASSETS_DIR = UI_DIR / "assets"

SHELL_SOURCE = (UI_DIR / "shell.py").read_text(encoding="utf-8")
APP_JS = (ASSETS_DIR / "app.js").read_text(encoding="utf-8")
APP_CSS = (ASSETS_DIR / "app.css").read_text(encoding="utf-8")
MARK_SVG = (ASSETS_DIR / "cyborg-mark.svg").read_text(encoding="utf-8")

EXTERNAL_URL = re.compile(r"https?://", re.IGNORECASE)
EXTERNAL_URL_FULL = re.compile(r"https?://[^\s'\"]+")
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)", re.IGNORECASE)
INLINE_HANDLER = re.compile(r"\son[a-z]+\s*=", re.IGNORECASE)
ASSET_REF = re.compile(r'(?:src|href)="([^"]+)"')
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

REQUIRED_IDS = (
    "dpyro-app",
    "rail",
    "sidebar",
    "stage",
    "inspector",
    "message-list",
    "composer-input",
    "composer-send",
    "composer-stop",
    "session-list",
    "palette-overlay",
    "palette-search",
    "new-session-overlay",
    "new-session-cwd",
    "new-session-form",
    "new-session-error",
    "palette-results",
    "settings-overlay",
    "settings-nav-list",
    "settings-body",
    "toast-root",
    "connection-state",
)

REQUIRED_ENDPOINTS = (
    "/api/ui/bootstrap",
    "/api/ui/sessions",
    "/api/ui/sessions/",
    "/api/sessions/",
    "/resume",
    "/messages",
    "/interrupt",
    "/timeline",
    "/events",
    "/transcript",
    "/api/ui/commands",
    "/api/ui/settings",
    "/api/ui/settings/agent",
    "/api/ui/settings/provider/",
    "/api/ui/settings/providers/",
    "/api/ui/settings/permission",
    "/api/ui/approvals",
    "/api/ui/plugins/",
    "/api/ui/config/agent",
    "/api/ui/config/provider/",
    "/api/ui/config/test",
    "/api/ui/permission",
)

SSE_EVENT_TYPES = (
    "session",
    "status",
    "step",
    "text",
    "thinking",
    "tool_call",
    "tool_result",
    "final",
    "usage",
    "error",
    "approval",
)

SNAKE_CASE_FIELDS = (
    "session_id",
    "active_session_id",
    "read_only",
    "context_tokens",
    "context_window",
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "call_id",
    "is_error",
    "approval_id",
    "tool_name",
    "created_at",
    "updated_at",
    "base_url",
    "api_key_set",
)

SETTINGS_SECTIONS = (
    "provider",
    "agent",
    "permissions",
    "commands",
    "plugins",
    "sessions",
    "web",
    "about",
)

MOCK_MARKERS = (
    "native-runtime",
    "web-surface",
    "eval-room",
    "design-lab",
    "scout-1",
    "sk-demo",
    "3178 passed",
    "42.6 MB",
    "4fa3bfe8",
    "webui-p0",
    "/brand/",
    "MOCK",
    "WAITING",
    "sk-demo-7f3a9c1e4b2d",
)


def test_build_shell_returns_a_complete_document() -> None:
    html = build_shell(path="/", version="0.3.0")

    assert html.startswith("<!doctype html>")
    assert '<html lang="en">' in html
    for part in ("<head>", "</head>", "<body>", "</body>", "</html>"):
        assert part in html
    assert "<title>DeepPyro</title>" in html
    assert 'name="viewport"' in html
    assert 'name="color-scheme"' in html
    assert 'content="0.3.0"' in html
    assert f'href="{ASSET_CSS}"' in html
    assert f'src="{ASSET_JS}"' in html
    assert f'href="{ASSET_MARK}"' in html
    assert f'href="{ASSET_FONT}"' in html
    assert "font/woff2" in html
    assert "defer" in html


def test_build_shell_body_is_a_self_contained_fragment() -> None:
    body = build_shell_body(path="/c/session-one", version="0.3.0")

    assert body.lstrip().startswith('<link rel="stylesheet"')
    for part in ("<!doctype", "<html", "<head>", "<body>", "<title>"):
        assert part not in body.lower()
    assert f'href="{ASSET_CSS}"' in body
    assert f'src="{ASSET_JS}"' in body
    assert 'id="dpyro-app"' in body
    assert 'data-view="chat"' in body
    assert 'data-session-id="session-one"' in body
    assert "noscript" in body
    # Initial state comes from GET /api/ui/bootstrap, never from an inline payload.
    assert "window." not in body
    assert "__DPYRO" not in body
    assert INLINE_SCRIPT.search(body) is None


def test_shell_has_the_four_regions_and_stable_control_ids() -> None:
    html = build_shell()

    for element_id in REQUIRED_IDS:
        assert f'id="{element_id}"' in html, f"missing element {element_id}"
    region_positions = [html.index(f'id="{region}"') for region in ("rail", "sidebar", "stage", "inspector")]
    assert region_positions == sorted(region_positions)
    assert html.count('class="app') == 1


def test_new_session_dialog_asks_for_a_workspace() -> None:
    html = build_shell()

    assert 'id="new-session-cwd"' in html
    assert 'placeholder="D:/path/to/project"' in html
    assert "Recent workspaces" in html
    assert "The current session stays in the sidebar" in html
    assert INLINE_SCRIPT.search(html) is None
    assert INLINE_HANDLER.search(html) is None


def test_every_route_renders_the_same_spa_frame() -> None:
    documents = (
        build_shell(path="/"),
        build_shell(path="/c/session-one"),
        build_shell(path="/palette"),
        build_shell(path="/settings"),
        build_shell(path="/settings/plugins"),
    )

    for html in documents:
        assert 'id="dpyro-app"' in html
        assert 'id="rail"' in html
        assert 'id="sidebar"' in html
        assert 'id="stage"' in html
        assert 'id="inspector"' in html
        assert f'src="{ASSET_JS}"' in html
    views = [re.search(r'data-view="([^"]+)"', html) for html in documents]
    assert [match.group(1) if match else None for match in views] == [
        "chat",
        "chat",
        "palette",
        "settings",
        "settings",
    ]


def test_shell_binds_local_assets_without_inline_code() -> None:
    html = build_shell()

    references = ASSET_REF.findall(html)
    assert {ASSET_CSS, ASSET_JS, ASSET_MARK, ASSET_FONT} <= set(references)
    for reference in references:
        assert reference == "#" or reference.startswith("/"), reference
        assert not reference.startswith("//"), reference
    assert EXTERNAL_URL.search(html) is None
    assert INLINE_SCRIPT.search(html) is None
    assert INLINE_HANDLER.search(html) is None
    assert "javascript:" not in html.lower()
    assert " style=" not in html
    assert "<style" not in html.lower()
    assert "nonce" not in html.lower()


@pytest.mark.parametrize(
    ("path", "view", "session_id", "section"),
    (
        ("/", ShellView.CHAT, None, None),
        ("/c/abc-123", ShellView.CHAT, "abc-123", None),
        ("/c/abc-123/extra", ShellView.CHAT, "abc-123", None),
        ("/palette", ShellView.PALETTE, None, None),
        ("/settings", ShellView.SETTINGS, None, DEFAULT_SETTINGS_SECTION),
        ("/settings/plugins", ShellView.SETTINGS, None, "plugins"),
        ("/settings/", ShellView.SETTINGS, None, DEFAULT_SETTINGS_SECTION),
    ),
)
def test_route_state_maps_frontend_views(
    path: str,
    view: ShellView,
    session_id: str | None,
    section: str | None,
) -> None:
    state = render_route_state(path=path)

    assert isinstance(state, ShellState)
    assert state.view is view
    assert state.session_id == session_id
    assert state.settings_section == section


def test_route_state_prefers_explicit_route_arguments() -> None:
    assert render_route_state(path="/c/from-path", session_id="explicit").session_id == "explicit"
    assert render_route_state(path="/settings/agent", section="plugins").settings_section == "plugins"
    assert render_route_state(path="/settings", section="web").settings_section == "web"


def test_route_state_document_titles_name_the_surface() -> None:
    assert render_route_state(path="/").document_title == "DeepPyro"
    assert "Command palette" in render_route_state(path="/palette").document_title
    title = render_route_state(path="/settings/permissions").document_title
    assert "permissions" in title
    assert "Settings" in title


def test_rendered_route_values_are_html_escaped() -> None:
    hostile_session = 'x"><script>alert(1)</script>'
    session_html = build_shell(path="/c/" + hostile_session)
    assert "<script>alert" not in session_html
    assert "&lt;script&gt;" in session_html
    assert INLINE_HANDLER.search(session_html) is None

    hostile_section = "/settings/<img src=x onerror=alert(1)>"
    settings_html = build_shell(path=hostile_section)
    assert "<img src=x" not in settings_html
    assert "&lt;img src=x onerror=alert(1)&gt;" in settings_html
    assert 'onerror="' not in settings_html

    version_html = build_shell(version='9"><b>')
    assert '9"><b>' not in version_html
    assert 'data-version="9&quot;&gt;&lt;b&gt;"' in version_html


def test_app_js_targets_every_live_api_endpoint() -> None:
    for endpoint in REQUIRED_ENDPOINTS:
        assert endpoint in APP_JS, f"app.js missing endpoint {endpoint}"
    assert "'/ui/app.css'" not in APP_JS  # stylesheet stays in the shell
    assert "/brand/" not in APP_JS
    urls = set(EXTERNAL_URL_FULL.findall(APP_JS))
    assert urls == {SVG_NAMESPACE}


def test_app_js_streams_every_contract_event_type() -> None:
    for event_type in SSE_EVENT_TYPES:
        assert f"type === '{event_type}'" in APP_JS, f"app.js does not handle SSE {event_type}"
    assert "/events" in APP_JS
    assert "new EventSource(" in APP_JS


def test_app_js_renders_live_bootstrap_sources() -> None:
    for field in (
        "data.sessions",
        "data.active_session_id",
        "data.commands",
        "data.presets",
        "data.plugins",
        "data.permission",
        "data.web",
        "data.about",
    ):
        assert field in APP_JS, f"app.js never renders live {field}"
    for state_name in ("state.sessions", "state.commands", "state.plugins", "state.providers"):
        assert state_name in APP_JS, f"app.js keeps no live {state_name}"
    assert "state.bootstrapped" in APP_JS
    assert "function openNewSession" in APP_JS
    assert "new-session-cwd" in APP_JS
    assert "state.defaultCwd" in APP_JS


def test_app_js_renders_with_text_nodes_only() -> None:
    for unsafe in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "javascript:",
        "srcdoc",
    ):
        assert unsafe not in APP_JS, f"app.js uses {unsafe}"
    for safe in ("'use strict'", "createElement", "textContent", "addEventListener", "fetch("):
        assert safe in APP_JS, f"app.js missing {safe}"
    assert chr(96) not in APP_JS


def test_app_js_reads_snake_case_wire_fields() -> None:
    for field in SNAKE_CASE_FIELDS:
        assert field in APP_JS, f"app.js never reads {field}"


def test_app_js_wires_approvals_composer_and_settings_forms() -> None:
    assert "/api/ui/approvals" in APP_JS
    assert "'?session_id='" in APP_JS
    assert "/api/ui/approvals/" in APP_JS
    assert "Allow once" in APP_JS
    assert "Deny" in APP_JS
    assert "composer-input" in APP_JS
    assert "interrupt" in APP_JS
    for section in SETTINGS_SECTIONS:
        assert f"id: '{section}'" in APP_JS, f"settings section {section} is not wired"
    for label in (
        "Provider and models",
        "Agent behavior",
        "Permissions",
        "Commands",
        "Plugins",
        "Sessions",
        "Web host",
        "About",
    ):
        assert label in APP_JS, f"settings label {label} is missing"


def test_app_css_is_packaged_and_hosts_the_spa() -> None:
    assert "url('/ui/InterVariable.woff2')" in APP_CSS
    assert "/brand/" not in APP_CSS
    assert "@import" not in APP_CSS
    assert EXTERNAL_URL.search(APP_CSS) is None
    assert APP_CSS.count("url(") == 1
    for rule in (
        "#app",
        "display: none",
        "#dpyro-app",
        "position: fixed",
        "inset: 0",
        "z-index: 1",
    ):
        assert rule in APP_CSS, f"app.css missing host rule {rule}"
    assert '#dpyro-app[data-view="palette"] #palette-overlay' in APP_CSS
    assert '#dpyro-app[data-view="settings"] #settings-overlay' in APP_CSS
    assert ".overlay-root.is-open" in APP_CSS
    assert ".new-session-modal" in APP_CSS
    assert (
        re.search(r"grid-template-columns:\s*72px\s+266px\s+minmax\(0,\s*1fr\)\s+306px", APP_CSS) is not None
    )
    assert "@media" in APP_CSS
    for component in (".composer-field", ".tool-card", ".approval", ".reasoning", ".toast"):
        assert component in APP_CSS, f"app.css missing {component}"


def test_assets_are_local_svg_and_font_files() -> None:
    for name in ("app.css", "app.js", "cyborg-mark.svg", "InterVariable.woff2"):
        assert (ASSETS_DIR / name).is_file(), f"missing asset {name}"
    assert (ASSETS_DIR / "InterVariable.woff2").read_bytes()[:4] == b"wOF2"
    assert MARK_SVG.lstrip().startswith("<svg")
    assert "viewBox" in MARK_SVG
    assert "<script" not in MARK_SVG.lower()
    assert set(EXTERNAL_URL_FULL.findall(MARK_SVG)) == {SVG_NAMESPACE}


def test_no_static_prototype_mock_data_survives() -> None:
    for name, text in (("shell.py", SHELL_SOURCE), ("app.js", APP_JS), ("app.css", APP_CSS)):
        for marker in MOCK_MARKERS:
            assert marker not in text, f"{name} still contains prototype marker {marker}"
    for fake_count in ("24 commands", "18 sessions", "$4.82", "21.8k", "39 web tests", "180.59"):
        assert fake_count not in SHELL_SOURCE
        assert fake_count not in APP_JS
