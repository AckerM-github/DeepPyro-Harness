"""Frontend asset guards for the loopback web shell.

These tests are static: they inspect the embedded page/CSS/JS constants and the
security header policy, so they need no socket and no browser. They pin the
properties the CSP and the plugin shell rely on: no inline script or style, no
CDN or build step, XSS-safe rendering with textContent, EventSource streaming,
snake_case wire fields, and a visible localhost banner.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from deeppyro.web import assets
from deeppyro.web.security import SECURITY_HEADERS

WEB_DIR = Path(__file__).resolve().parents[2] / "src" / "deeppyro" / "web"
EXTERNAL_URL = re.compile(r"https?://", re.IGNORECASE)


def _csp() -> str:
    return dict(SECURITY_HEADERS)["content-security-policy"]


def test_index_page_is_a_localhost_shell() -> None:
    page = assets.INDEX_HTML
    assert "Localhost only" in page
    assert "127.0.0.1" in page
    assert "<!doctype html>" in page.lower()
    assert 'name="viewport"' in page
    for element_id in (
        "connection",
        "new-session",
        "session-list",
        "session-title",
        "session-id",
        "status-model",
        "status-preset",
        "status-context",
        "status-usage",
        "transcript",
        "composer-input",
        "send",
        "stop",
    ):
        assert f'id="{element_id}"' in page, f"missing element {element_id}"
    assert "Ctrl+Enter" in page
    assert assets.TOKEN_PLACEHOLDER in page
    assert page.count(assets.TOKEN_PLACEHOLDER) >= 2  # both asset links carry the token


def test_index_page_has_no_inline_script_style_or_handlers() -> None:
    page = assets.INDEX_HTML
    assert "<script>" not in page
    assert "<style" not in page
    assert " style=" not in page
    assert " onclick=" not in page
    assert " onload=" not in page
    assert "javascript:" not in page


def test_index_page_has_no_external_urls() -> None:
    assert not EXTERNAL_URL.search(assets.INDEX_HTML)
    assert not EXTERNAL_URL.search(assets.APP_CSS)
    assert not EXTERNAL_URL.search(assets.APP_JS)
    page = assets.INDEX_HTML
    assert "/assets/app.css" in page
    assert "/assets/app.js" in page
    assert "fonts." not in page
    assert "googleapis" not in page


def test_index_html_injects_an_escaped_token_query() -> None:
    page = assets.index_html("tok_abc-123")
    assert assets.TOKEN_PLACEHOLDER not in page
    assert "token=tok_abc-123" in page
    assert page.count("tok_abc-123") >= 2
    escaped = assets.index_html("a&b=c")
    assert assets.TOKEN_PLACEHOLDER not in escaped
    assert "&" not in escaped.split("token=")[-1][:8]
    assert "token=a%26b%3Dc" in escaped


def test_csp_allows_only_same_origin_assets() -> None:
    csp = _csp()
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert not EXTERNAL_URL.search(csp)


def test_stylesheet_is_embedded_responsive_and_font_free() -> None:
    css = assets.APP_CSS
    assert "@import" not in css
    assert "@font-face" not in css
    assert "url(" not in css
    assert "@media" in css
    assert "grid" in css
    assert "prefers-color-scheme" in css or "--bg" in css


def test_javascript_never_renders_markup() -> None:
    js = assets.APP_JS
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert forbidden not in js, f"frontend uses {forbidden}"
    assert "textContent" in js
    assert "createElement" in js
    assert "'use strict'" in js


def test_javascript_streams_with_eventsource_and_fetch() -> None:
    js = assets.APP_JS
    assert "new EventSource(" in js
    assert "fetch(" in js
    assert "'authorization': 'Bearer ' + token" in js
    assert "&after=" in js
    assert "data.seq" in js
    assert "/events?token=" in js
    for endpoint in ("/api/sessions", "/transcript", "/resume", "/messages", "/interrupt"):
        assert endpoint in js, f"missing endpoint {endpoint}"
    assert "Connection" in js or "connection" in js
    assert "state.source.close()" in js


def test_javascript_has_keyboard_shortcuts_and_controls() -> None:
    js = assets.APP_JS
    assert "keydown" in js
    assert "ctrlKey" in js
    assert "metaKey" in js
    assert "Escape" in js
    assert "interruptTurn" in js
    assert "createSession" in js
    assert "resumeSession" in js
    assert "connectStream" in js


def test_wire_fields_stay_snake_case_in_the_browser_boundary() -> None:
    js = assets.APP_JS
    for field in (
        "session_id",
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "context_tokens",
        "context_window",
        "call_id",
        "is_error",
        "active_session_id",
        "read_only",
        "queued",
    ):
        assert field in js, f"missing snake_case field {field}"
    for camel in (
        "event.callId",
        "event.isError",
        "event.inputTokens",
        "data.sessionId",
        "data.callId",
        "data.inputTokens",
        "data.contextTokens",
    ):
        assert camel not in js, f"camelCase wire field leaked into JavaScript: {camel}"


def test_javascript_has_no_build_step_dependency() -> None:
    js = assets.APP_JS
    for forbidden in ("import ", "require(", "node_modules", "unpkg", "jsdelivr"):
        assert forbidden not in js, f"frontend depends on build tooling: {forbidden}"
    assert "package.json" not in js


def test_assets_are_plain_module_constants() -> None:
    source = (WEB_DIR / "assets.py").read_text(encoding="utf-8")
    for name in ("INDEX_HTML", "APP_CSS", "APP_JS"):
        value = getattr(assets, name)
        assert isinstance(value, str) and value.strip(), f"{name} is not an embedded string"
    tree = ast.parse(source, filename=str(WEB_DIR / "assets.py"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"open", "exec", "eval", "compile"}, (
                "assets.py performs dynamic code/file IO"
            )
    assert "read_text" not in source
    assert "Path(" not in source
    assert assets.index_html("token") != assets.INDEX_HTML
