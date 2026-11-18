"""Tests for the cross-process settings lock (KI-021)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from deeppyro.kernel.manifest import PluginSpec
from deeppyro.plugins import (
    PluginError,
    SettingsLockTimeout,
    SettingsStore,
    read_manifest,
    save_user_manifest,
)
from deeppyro.plugins.file_lock import (
    DEFAULT_TIMEOUT_SECONDS,
    FileLock,
    LockError,
    LockTimeout,
    file_lock,
    lock_path_for,
)

TARGET = "tests.builtin.demo_plugins:DemoPlugin"


def seed(path: Path, *specs: PluginSpec) -> Path:
    save_user_manifest(path, list(specs))
    return path


@pytest.fixture()
def manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    path = tmp_path / "plugins.toml"
    seed(path, PluginSpec(id="demo-plugin", name=TARGET, config={"apiKey": "hunter2", "maxTokens": 1234}))
    return path


def test_file_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    path = tmp_path / "plugins.toml.lock"
    first = FileLock(path)
    second = FileLock(path, timeout=0.2, poll=0.01)

    assert not first.held and not second.held
    first.acquire()
    try:
        assert first.held
        started = time.monotonic()
        with pytest.raises(LockTimeout) as caught:
            second.acquire()
        assert time.monotonic() - started >= 0.15
        assert caught.value.path == path
        assert caught.value.timeout == pytest.approx(0.2)
        assert "waiting for lock" in str(caught.value)
        assert not second.held
    finally:
        first.release()
    assert not first.held
    first.release()  # idempotent

    second.acquire()
    try:
        assert second.held
    finally:
        second.release()


def test_file_lock_timeout_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    holder = FileLock(path)
    holder.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(LockTimeout):
            FileLock(path, timeout=0.1, poll=0.01).acquire()
        elapsed = time.monotonic() - started
        assert 0.08 <= elapsed < 2.0

        started = time.monotonic()
        with pytest.raises(LockTimeout):
            FileLock(path, timeout=0).acquire()
        assert time.monotonic() - started < 1.0
    finally:
        holder.release()


def test_file_lock_timeout_defaults_and_negative_values() -> None:
    lock = FileLock(Path("unused.lock"))
    assert lock.timeout == DEFAULT_TIMEOUT_SECONDS
    assert FileLock(Path("unused.lock"), timeout=-3).timeout == 0.0


def test_file_lock_released_after_exception(tmp_path: Path) -> None:
    path = tmp_path / "state.lock"
    with pytest.raises(RuntimeError, match="boom"), FileLock(path, timeout=0.2) as lock:
        assert lock.held
        raise RuntimeError("boom")

    again = FileLock(path, timeout=0)
    again.acquire()
    try:
        assert again.held
    finally:
        again.release()


def test_file_lock_reports_unusable_lock_path(tmp_path: Path) -> None:
    blocker = tmp_path / "blocked"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    with pytest.raises(LockError, match="cannot open lock file"):
        FileLock(blocker / "state.lock", timeout=0).acquire()


def test_lock_path_for_manifest_and_no_toml_pollution(manifest: Path) -> None:
    expected = manifest.with_name("plugins.toml.lock")
    assert lock_path_for(manifest) == expected
    assert SettingsStore(manifest_path=manifest).lock_path == expected

    before = manifest.read_bytes()
    with file_lock(manifest):
        # A held byte-range lock also blocks reading the locked byte on
        # Windows, so check the size instead of opening the lock file.
        assert expected.is_file()
        assert expected.stat().st_size == 0

    assert expected.read_bytes() == b""
    assert manifest.read_bytes() == before
    assert read_manifest(manifest)[0].config == {"apiKey": "hunter2", "maxTokens": 1234}


def test_write_times_out_behind_held_lock(manifest: Path) -> None:
    store = SettingsStore(manifest_path=manifest, lock_timeout=0.2)
    revision = store.read("demo-plugin")["revision"]
    with file_lock(manifest, timeout=0.2), pytest.raises(SettingsLockTimeout, match="another process"):
        store.write("demo-plugin", {"model": "m2"}, revision)

    written = store.write("demo-plugin", {"model": "m2"}, revision)
    assert written["changed"] == ["model"]


def test_lock_error_is_a_plugin_error() -> None:
    assert issubclass(SettingsLockTimeout, PluginError)


CHILD_TIMEOUT_SCRIPT = """
import sys
from pathlib import Path

from deeppyro.plugins import SettingsLockTimeout, SettingsStore

store = SettingsStore(manifest_path=Path(sys.argv[1]), lock_timeout=0.3)
revision = store.read("demo-plugin")["revision"]
try:
    store.write("demo-plugin", {"model": "from-child"}, revision)
except SettingsLockTimeout:
    print("timeout")
    raise SystemExit(0)
print("wrote")
raise SystemExit(1)
"""


def _subprocess_env(tmp_path: Path) -> dict[str, str]:
    repo = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    env["DEEPPYRO_HOME"] = str(tmp_path / "home")
    return env


def test_other_process_gets_settings_lock_timeout(manifest: Path, tmp_path: Path) -> None:
    with file_lock(manifest, timeout=0.2):
        completed = subprocess.run(
            [sys.executable, "-c", CHILD_TIMEOUT_SCRIPT, str(manifest)],
            cwd=tmp_path,
            env=_subprocess_env(tmp_path),
            capture_output=True,
            text=True,
            timeout=120,
        )
    assert completed.returncode == 0, completed.stderr
    assert "timeout" in completed.stdout
    assert read_manifest(manifest)[0].config.get("model") != "from-child"


CONCURRENT_WRITER_SCRIPT = """
import sys
import time
from pathlib import Path

from deeppyro.plugins import SettingsConflict, SettingsStore

store = SettingsStore(manifest_path=Path(sys.argv[1]))
for _ in range(int(sys.argv[2])):
    for _attempt in range(500):
        read = store.read("demo-plugin")
        current = store.effective_config("demo-plugin")["maxTokens"]
        try:
            store.write("demo-plugin", {"maxTokens": current + 1}, read["revision"])
        except SettingsConflict:
            time.sleep(0.005)
            continue
        break
    else:
        raise SystemExit("gave up after 500 conflicting attempts")
print("done")
"""


def test_concurrent_processes_do_not_lose_updates(manifest: Path, tmp_path: Path) -> None:
    env = _subprocess_env(tmp_path)
    command = [sys.executable, "-c", CONCURRENT_WRITER_SCRIPT, str(manifest), "10"]
    first = subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    second = subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    outputs = []
    for process in (first, second):
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr.decode()
        outputs.append(stdout.decode())
    assert [output.strip() for output in outputs] == ["done", "done"]
    assert read_manifest(manifest)[0].config["maxTokens"] == 1234 + 20
