"""Tests for the Windows ACL restricted-token runner.

The CLI parsing and failure contract are tested on every platform. Token and
ACL behaviour needs a real Windows host and is skipped elsewhere; those tests
spawn the module itself through subprocess so the runner is exercised exactly
as LocalSandboxProvider will call it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from deeppyro.sandbox import windows_acl_runner as runner
from deeppyro.sandbox.windows_acl_runner import (
    EXIT_RUNNER_FAILURE,
    RunnerError,
    parse_cli,
    self_command,
    workspace_write_sid,
)

IS_WINDOWS = os.name == "nt"
WINDOWS_ONLY = pytest.mark.skipif(not IS_WINDOWS, reason="requires the Windows ACL runner")

REPO_ROOT = Path(__file__).resolve().parents[2]
# The venv python is a uv trampoline that prints a harmless warning when it
# cannot chdir under the restricted token; use the base interpreter for children
# whose stderr must be exact.
CHILD_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
SID_PATTERN = re.compile(r"^S-1-4-(\d+)-(\d+)$")


def _icacls_grant(path: Path, spec: str) -> None:
    subprocess.run(["icacls", str(path), "/grant", spec], capture_output=True, check=False, timeout=60)


@pytest.fixture()
def acl_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "acl"
    workspace = root / "workspace"
    temp = root / "temp"
    outside = root / "outside"
    for directory in (workspace, temp, outside):
        directory.mkdir(parents=True)
    if IS_WINDOWS:
        # Pytest temp directories are private to the local account SID, which a
        # restricted token cannot match during the normal access check. Real
        # workspaces grant Authenticated Users Modify, so mirror that: traverse
        # on the root, and a non-inherited Modify ACE on the two directories the
        # runner is allowed to write. The synthetic workspace SID is still
        # required for the restricted access check, so this does not weaken the
        # confinement being tested (outside gets no write ACE).
        _icacls_grant(root, "*S-1-1-0:(OI)(CI)(RX)")
        _icacls_grant(workspace, "*S-1-5-11:(M)")
        _icacls_grant(temp, "*S-1-5-11:(M)")
    return workspace, temp, outside


def run_runner(*args: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*self_command(), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def valid_argv(workspace: Path, temp: Path, mode: str = "read-only") -> list[str]:
    return [
        "--workspace",
        str(workspace),
        "--temp",
        str(temp),
        "--mode",
        mode,
        "--",
        "cmd",
    ]


def assert_runner_error(argv: list[str]) -> None:
    with pytest.raises(RunnerError):
        parse_cli(argv)


def acl_text(path: Path) -> str:
    completed = subprocess.run(["icacls", str(path)], capture_output=True, timeout=60, check=False)
    assert completed.returncode == 0
    return completed.stdout.decode("utf-8", errors="replace")


def child_writer_script(workspace: Path, temp: Path, outside: Path) -> str:
    lines = [
        "import pathlib",
        "import sys",
        "workspace = pathlib.Path(" + repr(str(workspace)) + ")",
        "temp = pathlib.Path(" + repr(str(temp)) + ")",
        "outside = pathlib.Path(" + repr(str(outside)) + ")",
        "def attempt(path):",
        "    try:",
        "        path.write_text('payload', encoding='utf-8')",
        "        return 'OK'",
        "    except PermissionError:",
        "        return 'DENIED'",
        "print('workspace=' + attempt(workspace / 'inside.txt'))",
        "print('temp=' + attempt(temp / 'temp.txt'))",
        "print('outside=' + attempt(outside / 'outside.txt'))",
        "sys.exit(0)",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# self_command and SID derivation (pure functions, every platform)
# ---------------------------------------------------------------------------


def test_self_command_reinvokes_this_module() -> None:
    command = self_command()
    assert command == [sys.executable, "-m", "deeppyro.sandbox.windows_acl_runner"]
    assert all(isinstance(part, str) for part in command)


def test_workspace_write_sid_is_stable_and_well_formed(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, _, _ = acl_dirs
    sid = workspace_write_sid(workspace)
    match = SID_PATTERN.match(sid)
    assert match is not None, sid
    first, second = (int(value) for value in match.groups())
    assert 1 <= first <= 2**30 - 1
    assert 1 <= second <= 2**30 - 1

    assert workspace_write_sid(str(workspace)) == sid
    assert workspace_write_sid(workspace / ".." / workspace.name) == sid
    assert workspace_write_sid(str(workspace) + os.sep) == sid


def test_workspace_write_sid_differs_per_workspace(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    assert workspace_write_sid(workspace) != workspace_write_sid(temp)


# ---------------------------------------------------------------------------
# CLI parsing
# ---------------------------------------------------------------------------


def test_parse_cli_accepts_separate_values(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    parsed_workspace, parsed_temp, mode, child = parse_cli(
        [
            "--workspace",
            str(workspace),
            "--temp",
            str(temp),
            "--mode",
            "workspace-write",
            "--",
            "python",
            "-c",
            "print(1)",
        ]
    )
    assert parsed_workspace == Path(os.path.realpath(workspace))
    assert parsed_temp == Path(os.path.realpath(temp))
    assert mode == "workspace-write"
    assert child == ["python", "-c", "print(1)"]


def test_parse_cli_accepts_equals_form_and_preserves_child_flags(
    acl_dirs: tuple[Path, Path, Path],
) -> None:
    workspace, temp, _ = acl_dirs
    _, _, mode, child = parse_cli(
        [
            f"--workspace={workspace}",
            f"--temp={temp}",
            "--mode=read-only",
            "--",
            "python",
            "--mode",
            "not-ours",
        ]
    )
    assert mode == "read-only"
    assert child == ["python", "--mode", "not-ours"]


def test_parse_cli_requires_separator(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    assert_runner_error(["--workspace", str(workspace), "--temp", str(temp), "--mode", "read-only"])


def test_parse_cli_requires_non_empty_child(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    assert_runner_error(valid_argv(workspace, temp)[:-1])


def test_parse_cli_requires_each_option(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    assert_runner_error(["--temp", str(temp), "--mode", "read-only", "--", "cmd"])
    assert_runner_error(["--workspace", str(workspace), "--mode", "read-only", "--", "cmd"])
    assert_runner_error(["--workspace", str(workspace), "--temp", str(temp), "--", "cmd"])


def test_parse_cli_rejects_unknown_and_duplicate_options(
    acl_dirs: tuple[Path, Path, Path],
) -> None:
    workspace, temp, _ = acl_dirs
    assert_runner_error(["--bogus", "x", *valid_argv(workspace, temp)])
    duplicate = valid_argv(workspace, temp)
    duplicate.insert(0, "--mode")
    duplicate.insert(1, "read-only")
    assert_runner_error(duplicate)


def test_parse_cli_rejects_missing_or_empty_option_values(
    acl_dirs: tuple[Path, Path, Path],
) -> None:
    workspace, temp, _ = acl_dirs
    assert_runner_error(["--workspace"])
    assert_runner_error(["--workspace=", "--temp", str(temp), "--mode", "read-only", "--", "cmd"])
    assert_runner_error(["--workspace", str(workspace), "--temp", "--", "cmd"])


def test_parse_cli_rejects_invalid_mode(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    assert_runner_error(valid_argv(workspace, temp, "danger-full-access"))


def test_parse_cli_rejects_bad_directories(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    missing = workspace / "missing"
    assert_runner_error(valid_argv(missing, temp))
    assert_runner_error(valid_argv(workspace, missing))
    file_path = workspace / "file.txt"
    file_path.write_text("x", encoding="utf-8")
    assert_runner_error(valid_argv(file_path, temp))
    assert_runner_error(valid_argv(workspace, file_path))
    assert_runner_error(valid_argv(workspace, workspace))


# ---------------------------------------------------------------------------
# Fail-closed contract through the real CLI
# ---------------------------------------------------------------------------


def test_cli_failure_exits_127_with_prefixed_stderr(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    marker = workspace / "should-not-exist.txt"
    child = [CHILD_PYTHON, "-c", "open(" + repr(str(marker)) + ", 'w').write('x')"]
    # Missing the '--' separator, so the child argv must never start.
    result = run_runner("--workspace", str(workspace), "--temp", str(temp), "--mode", "read-only", *child)
    assert result.returncode == EXIT_RUNNER_FAILURE
    assert result.stderr.startswith("windows-acl-run: ")
    assert not marker.exists()


@pytest.mark.parametrize(
    "tail",
    [
        ["--"],
        ["--mode", "bogus", "--", "cmd"],
        ["--bogus", "x", "--", "cmd"],
    ],
)
def test_cli_rejects_bad_input_without_starting_child(
    acl_dirs: tuple[Path, Path, Path], tail: list[str]
) -> None:
    workspace, temp, _ = acl_dirs
    result = run_runner("--workspace", str(workspace), "--temp", str(temp), "--mode", "read-only", *tail)
    assert result.returncode == EXIT_RUNNER_FAILURE
    assert result.stderr.startswith("windows-acl-run: ")


@WINDOWS_ONLY
def test_main_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, acl_dirs: tuple[Path, Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    workspace, temp, _ = acl_dirs

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RunnerError("synthetic failure")

    monkeypatch.setattr(runner, "_run_windows", boom)
    code = runner.main(valid_argv(workspace, temp))
    captured = capsys.readouterr()
    assert code == EXIT_RUNNER_FAILURE
    assert captured.err == "windows-acl-run: synthetic failure\n"


# ---------------------------------------------------------------------------
# Real restricted token and ACL behaviour (Windows only)
# ---------------------------------------------------------------------------


@WINDOWS_ONLY
def test_workspace_write_confines_child_writes(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, outside = acl_dirs
    script = child_writer_script(workspace, temp, outside)
    result = run_runner(
        "--workspace",
        str(workspace),
        "--temp",
        str(temp),
        "--mode",
        "workspace-write",
        "--",
        CHILD_PYTHON,
        "-c",
        script,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "workspace=OK",
        "temp=OK",
        "outside=DENIED",
    ]
    assert (workspace / "inside.txt").read_text(encoding="utf-8") == "payload"
    assert (temp / "temp.txt").read_text(encoding="utf-8") == "payload"
    assert not (outside / "outside.txt").exists()


@WINDOWS_ONLY
def test_read_only_denies_all_writes(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, outside = acl_dirs
    script = child_writer_script(workspace, temp, outside)
    result = run_runner(
        "--workspace",
        str(workspace),
        "--temp",
        str(temp),
        "--mode",
        "read-only",
        "--",
        CHILD_PYTHON,
        "-c",
        script,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "workspace=DENIED",
        "temp=DENIED",
        "outside=DENIED",
    ]
    assert not (workspace / "inside.txt").exists()
    assert not (temp / "temp.txt").exists()
    assert not (outside / "outside.txt").exists()


@WINDOWS_ONLY
def test_child_exit_code_is_mirrored(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    result = run_runner(
        "--workspace",
        str(workspace),
        "--temp",
        str(temp),
        "--mode",
        "read-only",
        "--",
        CHILD_PYTHON,
        "-c",
        "raise SystemExit(7)",
    )
    assert result.returncode == 7
    assert result.stderr == ""


@WINDOWS_ONLY
def test_child_stdio_is_inherited(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    script = "import sys\nprint('hello')\nsys.stderr.write('oops')\n"
    result = run_runner(
        "--workspace",
        str(workspace),
        "--temp",
        str(temp),
        "--mode",
        "read-only",
        "--",
        CHILD_PYTHON,
        "-c",
        script,
    )
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == "oops"


@WINDOWS_ONLY
def test_workspace_grant_is_standing_and_temp_grant_is_revoked(
    acl_dirs: tuple[Path, Path, Path],
) -> None:
    workspace, temp, _ = acl_dirs
    sid = workspace_write_sid(workspace)
    argv = [
        "--workspace",
        str(workspace),
        "--temp",
        str(temp),
        "--mode",
        "workspace-write",
        "--",
        CHILD_PYTHON,
        "-c",
        "pass",
    ]
    first = run_runner(*argv)
    assert first.returncode == 0, first.stderr

    workspace_acl = acl_text(workspace)
    temp_acl = acl_text(temp)
    assert workspace_acl.count(sid) == 1, workspace_acl
    assert sid not in temp_acl, temp_acl

    # A second call must not duplicate the standing ACE.
    second = run_runner(*argv)
    assert second.returncode == 0, second.stderr
    assert acl_text(workspace).count(sid) == 1


@WINDOWS_ONLY
def test_read_only_does_not_touch_workspace_acl(acl_dirs: tuple[Path, Path, Path]) -> None:
    workspace, temp, _ = acl_dirs
    sid = workspace_write_sid(workspace)
    result = run_runner(
        "--workspace",
        str(workspace),
        "--temp",
        str(temp),
        "--mode",
        "read-only",
        "--",
        CHILD_PYTHON,
        "-c",
        "pass",
    )
    assert result.returncode == 0, result.stderr
    assert sid not in acl_text(workspace)
