"""Frozen-build behaviour: resource location and the hidden re-entry flags.

A PyInstaller build sets ``sys.frozen`` and unpacks bundled data into
``sys._MEIPASS``.  There is no frozen interpreter in CI, so these tests fake
that state to cover the branches a built executable actually takes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deeppyro import runtime_env
from deeppyro.kernel import composition
from deeppyro.ptc import worker as ptc_worker
from deeppyro.ptc.runtime import WORKER_PATH, PtcRuntime
from deeppyro.sandbox import windows_acl_runner
from deeppyro.tui import main as tui_main


@pytest.fixture()
def frozen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pretend this interpreter is a onefile build unpacked into a temp dir."""
    root = tmp_path / "_MEI424242"
    root.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    return root


@pytest.fixture()
def source_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend a plain source install: no frozen marker at all."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)


# -- runtime_env ----------------------------------------------------------


def test_source_mode_resolves_inside_the_package(source_mode: None) -> None:
    package = Path(runtime_env.__file__).resolve().parent
    assert runtime_env.is_frozen() is False
    assert runtime_env.bundle_dir() == package
    relative = Path("bundles") / "core" / "bundle.toml"
    assert runtime_env.data_path(relative) == package / relative


def test_frozen_mode_resolves_into_meipass(frozen: Path) -> None:
    assert runtime_env.is_frozen() is True
    assert runtime_env.bundle_dir() == frozen
    relative = Path("bundles") / "core" / "bundle.toml"
    assert runtime_env.data_path(relative) == frozen / relative


def test_frozen_without_meipass_falls_back_to_the_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "dpyro.exe"))
    assert runtime_env.bundle_dir() == tmp_path


# -- composition ---------------------------------------------------------


def test_core_bundle_path_in_source_mode(source_mode: None) -> None:
    package = Path(composition.__file__).resolve().parent.parent
    assert composition.core_bundle_path() == package / "bundles" / "core" / "bundle.toml"


def test_core_bundle_path_follows_the_frozen_root(frozen: Path) -> None:
    assert composition.core_bundle_path() == frozen / "bundles" / "core" / "bundle.toml"


# -- sandbox runner ------------------------------------------------------


def test_sandbox_self_command_keeps_the_module_form_in_source(source_mode: None) -> None:
    assert windows_acl_runner.self_command() == [
        sys.executable,
        "-m",
        "deeppyro.sandbox.windows_acl_runner",
    ]


def test_sandbox_self_command_uses_the_internal_flag_when_frozen(frozen: Path) -> None:
    assert windows_acl_runner.self_command() == [sys.executable, runtime_env.INTERNAL_SANDBOX_RUNNER]


# -- PTC worker ----------------------------------------------------------


def test_ptc_worker_argv_in_source_mode(source_mode: None) -> None:
    runtime = PtcRuntime()
    assert runtime.uses_internal_worker() is False
    assert runtime.worker_argv() == [sys.executable, "-I", "-S", "-X", "utf8", str(WORKER_PATH)]


def test_ptc_worker_argv_when_frozen(frozen: Path) -> None:
    runtime = PtcRuntime()
    assert runtime.uses_internal_worker() is True
    assert runtime.worker_argv() == [sys.executable, runtime_env.INTERNAL_PTC_WORKER]


def test_ptc_explicit_worker_path_wins_over_the_internal_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    script = tmp_path / "worker.py"
    script.write_text("", encoding="utf-8")
    runtime = PtcRuntime(worker_path=script)
    assert runtime.uses_internal_worker() is False
    assert runtime.worker_argv() == [sys.executable, "-I", "-S", "-X", "utf8", str(script)]


# -- entry-point dispatch ------------------------------------------------


def test_internal_sandbox_flag_reaches_the_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_main(argv: list[str] | None = None) -> int:
        seen.append(list(argv or []))
        return 127

    monkeypatch.setattr(windows_acl_runner, "main", fake_main)
    assert tui_main.main([runtime_env.INTERNAL_SANDBOX_RUNNER, "--bogus"]) == 127
    assert seen == [["--bogus"]]


def test_internal_ptc_flag_skips_argparse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ptc_worker, "main", lambda *args, **kwargs: 7)
    assert tui_main.main([runtime_env.INTERNAL_PTC_WORKER]) == 7


def test_internal_flag_after_a_known_option_still_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    # On this path argparse sees the flag through REMAINDER; it must still reach
    # the worker instead of falling through to the runtime.
    monkeypatch.setattr(ptc_worker, "main", lambda *args, **kwargs: 7)
    argv = ["--headless", runtime_env.INTERNAL_PTC_WORKER, "--extra"]
    assert tui_main.main(argv) == 7


def test_internal_dispatch_does_not_touch_user_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from deeppyro import paths

    called: list[str] = []
    monkeypatch.setattr(paths, "migrate_legacy_layout", lambda: called.append("migrate"))
    monkeypatch.setattr(ptc_worker, "main", lambda *args, **kwargs: 0)
    assert tui_main.main([runtime_env.INTERNAL_PTC_WORKER]) == 0
    assert called == []


def test_help_hides_the_internal_flags() -> None:
    help_text = tui_main.build_parser().format_help()
    assert runtime_env.INTERNAL_SANDBOX_RUNNER not in help_text
    assert runtime_env.INTERNAL_PTC_WORKER not in help_text
