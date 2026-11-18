"""Cross-process advisory file locks for small JSON/TOML state files.

The settings store performs a read-modify-write cycle over the user patch
manifest.  The CAS revision token catches lost updates after the fact, but two
processes can still both read the same revision and then overwrite each other.
This module supplies the missing mutual exclusion: an advisory lock file next
to the target file (settings.toml -> settings.toml.lock) guarded by an OS
byte-range lock.

* Windows: msvcrt.locking with LK_NBLCK / LK_UNLCK.
* POSIX: fcntl.flock with LOCK_EX | LOCK_NB.

The lock is acquired in non-blocking mode and retried until a deadline, so
callers get a bounded wait and a LockTimeout instead of hanging forever.  The
lock file itself is never used for data and is never deleted: removing it
would race with other processes that already opened it (the classic advisory
lock unlink race).

These locks are advisory.  Every writer must use the same helper; the kernel
does not enforce anything.  That is sufficient here because all writers of the
settings manifest live in this package.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

#: Default bounded wait for a contended lock.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: Default sleep between retries while the lock is contended.
DEFAULT_POLL_SECONDS = 0.05

#: Number of bytes to lock.  One byte at offset 0 is enough for exclusion.
_LOCK_BYTES = 1

_LOCK_BUSY_ERRNOS = frozenset(
    code
    for code in (
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)


class LockError(RuntimeError):
    """The lock could not be acquired for a reason other than contention."""


class LockTimeout(LockError):
    """Timed out waiting for another process to release the lock."""

    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__(f"timed out after {timeout:.2f}s waiting for lock {path}")
        self.path = path
        self.timeout = timeout


if os.name == "nt":
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        """Try to take the OS lock without blocking; False if contended."""
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
        except OSError as exc:
            if exc.errno in _LOCK_BUSY_ERRNOS:
                return False
            raise
        return True

    def _unlock(handle: IO[bytes]) -> None:
        """Release the OS lock taken by _try_lock."""
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        """Try to take the OS lock without blocking; False if contended."""
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _LOCK_BUSY_ERRNOS:
                return False
            raise
        return True

    def _unlock(handle: IO[bytes]) -> None:
        """Release the OS lock taken by _try_lock."""
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class FileLock:
    """Advisory lock bound to a lock file, with a bounded acquire wait.

    The lock file is created (along with its parent directory) on demand and
    kept open for the lifetime of the lock.  acquire() retries a non-blocking
    OS lock until timeout elapses, then raises LockTimeout.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        poll: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._path = Path(path)
        self._timeout = max(0.0, float(timeout))
        self._poll = float(poll) if poll and poll > 0 else DEFAULT_POLL_SECONDS
        self._handle: IO[bytes] | None = None

    @property
    def path(self) -> Path:
        """The lock file this instance owns."""
        return self._path

    @property
    def timeout(self) -> float:
        """Maximum number of seconds acquire() waits for contention."""
        return self._timeout

    @property
    def held(self) -> bool:
        """Whether this instance currently owns the lock."""
        return self._handle is not None

    def acquire(self) -> None:
        """Take the lock, waiting at most timeout seconds."""
        if self._handle is not None:
            raise LockError(f"lock {self._path} is already held by this instance")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = self._path.open("a+b")
        except OSError as exc:
            raise LockError(f"cannot open lock file {self._path}: {exc}") from exc
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                if _try_lock(handle):
                    self._handle = handle
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockTimeout(self._path, self._timeout)
                time.sleep(min(self._poll, remaining))
        except BaseException:
            handle.close()
            raise

    def release(self) -> None:
        """Release the lock if held; safe to call more than once."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        except OSError:
            # The OS may already have dropped the lock when the handle was
            # closed (or the process is shutting down); closing is enough.
            pass
        finally:
            handle.close()

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        self.release()


def lock_path_for(target: str | Path) -> Path:
    """The lock file that protects target (settings.toml.lock)."""
    target_path = Path(target)
    return target_path.with_name(target_path.name + ".lock")


@contextmanager
def file_lock(
    target: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    poll: float = DEFAULT_POLL_SECONDS,
) -> Iterator[FileLock]:
    """Hold the cross-process lock that protects target.

    target is the data file, not the lock file: the lock file is derived with
    lock_path_for(), so callers cannot accidentally point the lock at a
    different directory than the data.
    """
    lock = FileLock(lock_path_for(target), timeout=timeout, poll=poll)
    with lock:
        yield lock


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "FileLock",
    "LockError",
    "LockTimeout",
    "file_lock",
    "lock_path_for",
]
