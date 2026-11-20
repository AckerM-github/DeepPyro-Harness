"""Tests for the static SPA host: shell fallback, cache policy, CSP and lifecycle."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from deeppyro.web.ui import server as ui_server
from deeppyro.web.ui.api import UiContext
from deeppyro.web.ui.auth import UI_CSP, cookie_name

TOKEN = "test-token-0123456789"
PORT = 8765
COOKIE = {"cookie": f"{cookie_name(PORT)}={TOKEN}"}

#: Markers that would mean the built bundle needs inline styles at runtime: an
#: inline <style> element, an HTML style attribute, a style setAttribute or a
#: CSSOM rule insertion. React style props are deliberately not listed: they
#: assign CSSStyleDeclaration properties, which CSP does not govern.
INLINE_STYLE_MARKERS = (
    "<style",
    ' style="',
    " style='",
    'setAttribute("style"',
    "setAttribute('style'",
    "cssText",
    "insertRule",
)


def _app(port: int = PORT) -> FastAPI:
    port_ref = ui_server._PortRef()
    port_ref.value = port
    return ui_server.create_app(
        service_provider=lambda: object(),
        context=UiContext(token=TOKEN, version="test"),
        port=port_ref,
    )


def _write_shell(directory: Path) -> None:
    """Write a minimal Vite-shaped build below the monkeypatched STATIC_DIR."""
    (directory / "index.html").write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    assets = directory / "assets"
    assets.mkdir()
    (assets / "index-deadbeef.js").write_text("console.log('build');", encoding="utf-8")
    (assets / "index-cafebabe.css").write_text("body{color:#fff}", encoding="utf-8")


def test_api_path_detection_is_exact() -> None:
    assert ui_server._is_api_path("api")
    assert ui_server._is_api_path("api/health")
    assert not ui_server._is_api_path("apiary")
    assert not ui_server._is_api_path("c/api/health")


def test_missing_build_returns_a_plain_503(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ui_server, "STATIC_DIR", tmp_path)
    response = ui_server._index_response()
    assert response.status_code == 503
    assert response.body == ui_server.MISSING_BUILD_MESSAGE.encode()
    assert response.headers["cache-control"] == ui_server.NO_STORE_CACHE_CONTROL


@pytest.mark.asyncio
async def test_spa_shell_serves_pages_but_never_the_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ui_server, "STATIC_DIR", tmp_path)
    (tmp_path / "index.html").write_text('<!doctype html><div id="root"></div>', encoding="utf-8")
    app = _app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=COOKIE) as client:
        shell = await client.get("/")
        assert shell.status_code == 200
        assert shell.headers["content-type"].startswith("text/html")
        assert 'id="root"' in shell.text

        deep_link = await client.get("/c/session-123")
        assert deep_link.status_code == 200
        assert 'id="root"' in deep_link.text

        missing_api = await client.get("/api/does-not-exist")
        assert missing_api.status_code == 404
        assert missing_api.headers["content-type"].startswith("application/json")
        assert missing_api.json()["error"]["code"] == "not_found"

        brand = await client.get("/brand/cyborg-mark.svg")
        assert brand.status_code == 200
        assert brand.headers["content-type"].startswith("image/svg")


@pytest.mark.asyncio
async def test_hashed_assets_are_immutable_while_shell_and_brand_are_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ui_server, "STATIC_DIR", tmp_path)
    _write_shell(tmp_path)
    app = _app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=COOKIE) as client:
        shell = await client.get("/")
        assert shell.status_code == 200
        assert shell.headers["cache-control"] == ui_server.NO_STORE_CACHE_CONTROL

        deep_link = await client.get("/c/session-123")
        assert deep_link.status_code == 200
        assert deep_link.headers["cache-control"] == ui_server.NO_STORE_CACHE_CONTROL

        brand = await client.get("/brand/cyborg-mark.svg")
        assert brand.status_code == 200
        assert brand.headers["cache-control"] == ui_server.NO_STORE_CACHE_CONTROL

        script = await client.get("/assets/index-deadbeef.js")
        assert script.status_code == 200
        assert script.headers["cache-control"] == ui_server.IMMUTABLE_CACHE_CONTROL

        stylesheet = await client.get("/assets/index-cafebabe.css")
        assert stylesheet.status_code == 200
        assert stylesheet.headers["cache-control"] == ui_server.IMMUTABLE_CACHE_CONTROL

        missing_asset = await client.get("/assets/index-missing.js")
        assert missing_asset.status_code == 404
        assert missing_asset.headers["cache-control"] == ui_server.NO_STORE_CACHE_CONTROL


def test_ui_csp_keeps_scripts_self_and_drops_inline_styles() -> None:
    assert "script-src 'self'" in UI_CSP
    assert "'unsafe-eval'" not in UI_CSP
    assert "style-src 'self'" in UI_CSP
    assert "'unsafe-inline'" not in UI_CSP


@pytest.mark.asyncio
async def test_every_static_reply_carries_the_ui_csp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ui_server, "STATIC_DIR", tmp_path)
    _write_shell(tmp_path)
    app = _app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1", headers=COOKIE) as client:
        for path in ("/", "/assets/index-deadbeef.js", "/brand/cyborg-mark.svg"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["content-security-policy"] == UI_CSP


def test_built_bundle_needs_no_inline_styles() -> None:
    """The checked-in Vite build must not need style-src 'unsafe-inline'."""
    index = ui_server.STATIC_DIR / "index.html"
    if not index.is_file():
        pytest.skip("SPA build missing")
    scripts = sorted((ui_server.STATIC_DIR / "assets").glob("*.js"))
    if not scripts:
        pytest.skip("SPA bundle missing")
    sources = [("index.html", index.read_text(encoding="utf-8"))]
    sources.extend((script.name, script.read_text(encoding="utf-8")) for script in scripts)
    for name, text in sources:
        for marker in INLINE_STYLE_MARKERS:
            assert marker not in text, (
                f"{name} contains {marker!r}; the built bundle may need style-src 'unsafe-inline'"
            )


@pytest.mark.asyncio
async def test_ui_server_starts_and_stops() -> None:
    server = ui_server.UiServer(
        token=TOKEN,
        service_provider=lambda: object(),
        context=UiContext(token=TOKEN, version="test"),
    )
    port = await server.start()
    try:
        assert port > 0
        assert server.address == ("127.0.0.1", port)
        assert server.url == f"http://127.0.0.1:{port}/"
        scoped = {"cookie": f"{cookie_name(port)}={TOKEN}"}
        async with httpx.AsyncClient(base_url=server.url) as client:
            tokenized = await client.get(f"/?token={TOKEN}")
            assert tokenized.status_code == 303
            assert tokenized.headers["location"] == "/"
            assert TOKEN not in tokenized.headers["location"]
            assert tokenized.headers["set-cookie"].startswith(f"{cookie_name(port)}=")

            response = await client.get("/", headers=scoped)
            assert response.status_code == 200
            assert 'id="root"' in response.text
            assert response.headers["cache-control"] == ui_server.NO_STORE_CACHE_CONTROL

            # A cookie minted for another port must never authenticate here.
            foreign = await client.get("/", headers={"cookie": f"{cookie_name(port + 1)}={TOKEN}"})
            assert foreign.status_code == 401
    finally:
        await server.close()
    assert server.closing
