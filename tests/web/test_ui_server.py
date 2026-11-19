"""The NiceGUI mount wiring: heartbeat tolerance and the reconnect window."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from deeppyro.web.ui import server as ui_server
from deeppyro.web.ui.api import UiContext


def test_the_ui_heartbeat_tolerates_a_slow_first_paint(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_run_with(app: FastAPI, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(ui_server.ui, "run_with", fake_run_with)
    monkeypatch.setattr(ui_server, "register_pages", lambda bridge: None)

    app = ui_server.create_app(
        service_provider=lambda: object(),
        context=UiContext(token="test-token", version="0.3.0"),
        port=ui_server._PortRef(),
    )

    assert isinstance(app, FastAPI)
    assert captured["app"] is app
    assert captured["reconnect_timeout"] == ui_server.RECONNECT_TIMEOUT_SECONDS
    assert float(captured["reconnect_timeout"]) >= 15.0
