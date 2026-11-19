"""Tests for the static SPA host: shell fallback, API separation and lifecycle."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from deeppyro.web.ui import server as ui_server
from deeppyro.web.ui.api import UiContext

TOKEN = "test-token-0123456789"
COOKIE = {"cookie": f"dpyro_web={TOKEN}"}


def _app() -> FastAPI:
    return ui_server.create_app(
        service_provider=lambda: object(),
        context=UiContext(token=TOKEN, version="test"),
        port=ui_server._PortRef(),
    )


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
        async with httpx.AsyncClient(base_url=server.url, headers=COOKIE) as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert 'id="root"' in response.text
    finally:
        await server.close()
    assert server.closing
