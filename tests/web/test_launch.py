"""The process launcher used after the TUI splash closes."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from deeppyro.web import launch as launch_module
from deeppyro.web.host import WebHost


class FakeHost:
    def __init__(self) -> None:
        self.stopped = False
        self.banner = ""

    def print_banner(self, stream: Any = None) -> None:
        self.banner = "URL: http://127.0.0.1:1234/?token=x"
        print(self.banner, file=stream)

    async def serve_forever(self) -> None:
        return None

    async def stop(self) -> None:
        self.stopped = True


async def test_serve_starts_and_stops_the_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHost()
    captured: dict[str, Any] = {}

    async def fake_start(cls: type[WebHost], **kwargs: Any) -> FakeHost:
        captured.update(kwargs)
        return fake

    monkeypatch.setattr(WebHost, "start", classmethod(fake_start))
    out = io.StringIO()
    await launch_module.serve(cwd=tmp_path, provider="deepseek", model="deepseek-flash", port=0, banner=out)

    assert captured["cwd"] == tmp_path
    assert captured["provider"] == "deepseek"
    assert captured["model"] == "deepseek-flash"
    assert captured["version"] == launch_module.__version__
    assert fake.stopped is True
    assert "token=" in out.getvalue()


def test_launch_returns_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FakeHost()

    async def fake_start(cls: type[WebHost], **kwargs: Any) -> FakeHost:
        return fake

    monkeypatch.setattr(WebHost, "start", classmethod(fake_start))
    assert launch_module.launch(cwd=tmp_path, banner=io.StringIO()) == 0
    assert fake.stopped is True


def test_launch_translates_ctrl_c(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(coro: Any) -> None:
        coro.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(launch_module.asyncio, "run", interrupted)
    err = io.StringIO()
    assert launch_module.launch(banner=err) == 130
    assert err.getvalue() == "\n"
