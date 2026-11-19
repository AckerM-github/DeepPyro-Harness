"""The TUI process entry point: splash F1 hands the process to the web host."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from deeppyro.tui import main as main_module


class FakeApp:
    instances: ClassVar[list[FakeApp]] = []
    result: ClassVar[Any] = "terminal"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        FakeApp.instances.append(self)

    def run(self) -> Any:
        return self.result


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    import deeppyro.paths

    monkeypatch.setattr(deeppyro.paths, "migrate_legacy_layout", lambda: None)
    FakeApp.instances.clear()


def test_splash_f1_launches_the_loopback_web_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeppyro.tui.app as app_module
    import deeppyro.web.launch as launch_module

    captured: dict[str, Any] = {}

    def fake_launch(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(app_module, "DeepPyroApp", FakeApp)
    monkeypatch.setattr(launch_module, "launch", fake_launch)
    FakeApp.result = "web"

    code = main_module.main(["--cwd", "D:/work", "--provider", "deepseek", "--model", "m1"])

    assert code == 0
    assert FakeApp.instances[0].kwargs == {
        "cwd": "D:/work",
        "provider": "deepseek",
        "model": "m1",
        "resume": None,
    }
    assert captured == {
        "cwd": "D:/work",
        "session_id": None,
        "provider": "deepseek",
        "model": "m1",
        "open_browser": True,
    }


def test_terminal_entry_point_returns_zero_without_starting_the_web_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeppyro.tui.app as app_module
    import deeppyro.web.launch as launch_module

    monkeypatch.setattr(app_module, "DeepPyroApp", FakeApp)

    def forbidden(**kwargs: Any) -> int:  # pragma: no cover - must not run
        raise AssertionError("the terminal entry point must not launch the web host")

    monkeypatch.setattr(launch_module, "launch", forbidden)
    FakeApp.result = "terminal"

    assert main_module.main([]) == 0


def test_splash_web_launch_forwards_the_resume_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deeppyro.tui.app as app_module
    import deeppyro.web.launch as launch_module

    captured: dict[str, Any] = {}

    def fake_launch(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(app_module, "DeepPyroApp", FakeApp)
    monkeypatch.setattr(launch_module, "launch", fake_launch)
    FakeApp.result = "web"

    assert main_module.main(["--resume", "sess_123"]) == 7
    assert captured["session_id"] == "sess_123"
