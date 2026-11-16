r"""Windows ACL restricted-token sandbox runner.

LocalSandboxProvider wraps a command as:

    python -m deeppyro.sandbox.windows_acl_runner --workspace DIR --temp DIR --mode read-only|workspace-write -- ARGV...

The runner is a standalone subprocess: it derives a synthetic workspace write
SID, grants an inheritable Modify ACE to the workspace (standing) and to the
private temp directory (revoked on exit), creates a write-restricted copy of
the current process token, starts the child with inherited stdio through
CreateProcessAsUserW(CREATE_SUSPENDED), assigns it to a kill-on-close job
object and mirrors the child exit code.

Every failure is fail-closed: the runner writes "windows-acl-run: <detail>"
to stderr, exits 127 and never starts an unconfined child.  ``self_command()``
picks the re-entry argv: ``-m`` for a source install, the hidden
``--internal-sandbox-runner`` flag for a frozen executable. The module can be
imported on any platform; only the Windows code path is executed under
os.name == "nt".
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import os
import struct
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from deeppyro.core.errors import DeeppyroError
from deeppyro.runtime_env import INTERNAL_SANDBOX_RUNNER, is_frozen

#: Prefix the provider recognises as a broken runner (never a command result).
FAILURE_PREFIX = "windows-acl-run: "
#: Exit status for a runner failure. The provider maps this to a sandbox error.
EXIT_RUNNER_FAILURE = 127
#: Modes this runner can enforce. danger-full-access never reaches the runner.
MODES: tuple[str, ...] = ("read-only", "workspace-write")

#: The restricted token is the only executor; a child is never started without it.
RESTRICTED_TOKEN_FLAGS = 13  # DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED


class RunnerError(DeeppyroError):
    """A runner failure that must abort the call without starting the child."""


def self_command() -> list[str]:
    """Return the argv that re-invokes this runner.

    A source install re-enters the module with ``-m``.  A frozen executable
    cannot: its ``sys.executable`` is the harness binary, so it re-enters
    through the hidden ``--internal-sandbox-runner`` flag that
    ``deeppyro.tui.main`` dispatches before booting the runtime.
    """
    if is_frozen():
        return [sys.executable, INTERNAL_SANDBOX_RUNNER]
    return [sys.executable, "-m", "deeppyro.sandbox.windows_acl_runner"]


def workspace_write_sid(workspace: str | Path) -> str:
    """Derive the synthetic write SID for one workspace path.

    Mirrors the reference implementation: sha256 of the canonical path's UTF-8
    bytes, two little-endian u32 values mapped into the S-1-4 authority.
    """
    canonical = os.path.realpath(os.fspath(workspace))
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    first = struct.unpack_from("<I", digest, 0)[0] % (2**30 - 1) + 1
    second = struct.unpack_from("<I", digest, 4)[0] % (2**30 - 1) + 1
    return f"S-1-4-{first}-{second}"


def parse_cli(argv: Sequence[str]) -> tuple[Path, Path, str, list[str]]:
    """Parse the runner CLI, returning (workspace, temp, mode, child argv)."""
    values: dict[str, str] = {}
    child: list[str] | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            child = list(argv[index + 1 :])
            break
        name, separator, inline = token.partition("=")
        if name not in ("--workspace", "--temp", "--mode"):
            raise RunnerError(f"unexpected argument: {token!r}")
        if name in values:
            raise RunnerError(f"{name} was specified more than once")
        if separator:
            value = inline
            index += 1
        else:
            if index + 1 >= len(argv):
                raise RunnerError(f"{name} requires a value")
            value = argv[index + 1]
            index += 2
        if not value:
            raise RunnerError(f"{name} requires a non-empty value")
        values[name] = value

    if child is None:
        raise RunnerError("missing '--' separator before the child argv")
    if not child:
        raise RunnerError("missing child argv after '--'")
    for required in ("--workspace", "--temp", "--mode"):
        if required not in values:
            raise RunnerError(f"missing required option {required}")

    mode = values["--mode"]
    if mode not in MODES:
        raise RunnerError(f'invalid --mode "{mode}": expected one of {", ".join(MODES)}')

    workspace = _require_directory(values["--workspace"], "--workspace")
    temp = _require_directory(values["--temp"], "--temp")
    if os.path.normcase(str(workspace)) == os.path.normcase(str(temp)):
        raise RunnerError("--workspace and --temp must be different directories")
    return workspace, temp, mode, child


def _require_directory(value: str, option: str) -> Path:
    path = Path(value).expanduser()
    if not path.exists():
        raise RunnerError(f'{option} directory does not exist: "{value}"')
    if not path.is_dir():
        raise RunnerError(f'{option} is not a directory: "{value}"')
    return Path(os.path.realpath(path))


def main(argv: Sequence[str] | None = None) -> int:
    """Run one confined command; return its exit code or the fail-closed 127."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        workspace, temp, mode, child = parse_cli(args)
    except RunnerError as exc:
        return _fail(str(exc))
    if os.name != "nt":
        return _fail(f"the Windows ACL runner requires Windows (os.name={os.name!r})")
    try:
        return _run_windows(workspace, temp, mode, child)
    except Exception as exc:  # noqa: BLE001 - every failure must become fail-closed
        detail = str(exc).strip() or type(exc).__name__
        return _fail(detail)


def _fail(detail: str) -> int:
    sys.stderr.write(f"{FAILURE_PREFIX}{detail.rstrip()}\n")
    sys.stderr.flush()
    return EXIT_RUNNER_FAILURE


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------
if os.name == "nt":
    from ctypes import wintypes

    TOKEN_ACCESS = 139  # TOKEN_ASSIGN_PRIMARY | TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ADJUST_DEFAULT
    TOKEN_GROUPS = 2
    TOKEN_DEFAULT_DACL = 6
    SE_GROUP_LOGON_ID = 0xC0000000

    WIN_WORLD_SID = 1
    SECURITY_MAX_SID_SIZE = 68

    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    GRANT_ACCESS = 1
    REVOKE_ACCESS = 4
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_UNKNOWN = 0
    SUB_CONTAINERS_AND_OBJECTS_INHERIT = 3

    #: Write + delete, displayed by icacls as Modify; deliberately no WRITE_DAC/WRITE_OWNER.
    GRANT_MASK = 1114454
    FILE_ALL_ACCESS = 2032127
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 8192
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    CREATE_SUSPENDED = 0x00000004
    STARTF_USESTDHANDLES = 0x00000100
    INFINITE = 0xFFFFFFFF
    WAIT_OBJECT_0 = 0
    HANDLE_FLAG_INHERIT = 0x00000001
    STD_INPUT_HANDLE = -10
    STD_OUTPUT_HANDLE = -11
    STD_ERROR_HANDLE = -12

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    def _win_call(ok, api: str, detail: str = "") -> None:
        if not ok:
            code = ctypes.get_last_error()
            suffix = f" ({detail})" if detail else ""
            raise RunnerError(f"{api} failed: {ctypes.WinError(code).strerror}{suffix}")

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", ctypes.c_void_p),
            ("MultipleTrusteeOperation", wintypes.DWORD),
            ("TrusteeForm", wintypes.DWORD),
            ("TrusteeType", wintypes.DWORD),
            ("ptstrName", ctypes.c_void_p),
        ]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", ctypes.c_int),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", TRUSTEE_W),
        ]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    def _check_structure_sizes() -> None:
        expected = (
            ("EXPLICIT_ACCESS_W", EXPLICIT_ACCESS_W, 48),
            ("SID_AND_ATTRIBUTES", SID_AND_ATTRIBUTES, 16),
            ("STARTUPINFOW", STARTUPINFOW, 104),
            ("PROCESS_INFORMATION", PROCESS_INFORMATION, 24),
        )
        for name, structure, size in expected:
            actual = ctypes.sizeof(structure)
            if actual != size:
                raise RunnerError(f"{name} has size {actual}, expected {size}")

    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetHandleInformation.restype = wintypes.BOOL
    kernel32.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.SetTokenInformation.restype = wintypes.BOOL
    advapi32.SetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    advapi32.CreateRestrictedToken.restype = wintypes.BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(SID_AND_ATTRIBUTES),
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.CreateWellKnownSid.restype = wintypes.BOOL
    advapi32.CreateWellKnownSid.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.CopySid.restype = wintypes.BOOL
    advapi32.CopySid.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
    advapi32.SetEntriesInAclW.restype = wintypes.DWORD
    advapi32.SetEntriesInAclW.argtypes = [
        wintypes.ULONG,
        ctypes.POINTER(EXPLICIT_ACCESS_W),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi32.CreateProcessAsUserW.restype = wintypes.BOOL
    advapi32.CreateProcessAsUserW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]

    def _open_current_token() -> wintypes.HANDLE:
        token = wintypes.HANDLE()
        _win_call(
            advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ACCESS, ctypes.byref(token)),
            "OpenProcessToken",
            f"access=0x{TOKEN_ACCESS:x}",
        )
        if not token.value:
            raise RunnerError("OpenProcessToken returned a null token")
        return token

    def _find_logon_sid(token: wintypes.HANDLE) -> bytes:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_GROUPS, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise RunnerError("GetTokenInformation(TokenGroups) size query returned 0")
        buffer = ctypes.create_string_buffer(needed.value)
        _win_call(
            advapi32.GetTokenInformation(token, TOKEN_GROUPS, buffer, needed.value, ctypes.byref(needed)),
            "GetTokenInformation",
            "TokenGroups",
        )
        count = struct.unpack_from("<I", buffer, 0)[0]
        for index in range(count):
            offset = 8 + index * 16
            sid_pointer = struct.unpack_from("<Q", buffer, offset)[0]
            attributes = struct.unpack_from("<I", buffer, offset + 8)[0]
            if sid_pointer and (attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID:
                length = advapi32.GetLengthSid(ctypes.c_void_p(sid_pointer))
                if length == 0:
                    raise RunnerError("GetLengthSid failed for the logon SID")
                copy = ctypes.create_string_buffer(length)
                _win_call(
                    advapi32.CopySid(length, copy, ctypes.c_void_p(sid_pointer)),
                    "CopySid",
                )
                return copy.raw[:length]
        raise RunnerError(f"no logon SID among {count} token groups")

    def _make_world_sid() -> bytes:
        buffer = ctypes.create_string_buffer(SECURITY_MAX_SID_SIZE)
        size = wintypes.DWORD(SECURITY_MAX_SID_SIZE)
        _win_call(
            advapi32.CreateWellKnownSid(WIN_WORLD_SID, None, buffer, ctypes.byref(size)),
            "CreateWellKnownSid",
            "WinWorldSid",
        )
        return buffer.raw[: size.value]

    def _sid_from_string(sid: str) -> bytes:
        pointer = ctypes.c_void_p()
        _win_call(
            advapi32.ConvertStringSidToSidW(sid, ctypes.byref(pointer)),
            "ConvertStringSidToSidW",
            sid,
        )
        try:
            length = advapi32.GetLengthSid(pointer)
            if length == 0:
                raise RunnerError(f"GetLengthSid failed for {sid}")
            return ctypes.string_at(pointer, length)
        finally:
            kernel32.LocalFree(pointer)

    def _explicit_access(sid_buffer: ctypes.Array, mode: int, mask: int) -> EXPLICIT_ACCESS_W:
        entry = EXPLICIT_ACCESS_W()
        entry.grfAccessPermissions = mask
        entry.grfAccessMode = mode
        entry.grfInheritance = SUB_CONTAINERS_AND_OBJECTS_INHERIT
        entry.Trustee.pMultipleTrustee = None
        entry.Trustee.MultipleTrusteeOperation = 0
        entry.Trustee.TrusteeForm = TRUSTEE_IS_SID
        entry.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
        entry.Trustee.ptstrName = ctypes.cast(sid_buffer, ctypes.c_void_p)
        return entry

    def _grant_dir_ace(path: Path, sid: bytes, mask: int = GRANT_MASK) -> None:
        sid_buffer = ctypes.create_string_buffer(sid, len(sid))
        entry = _explicit_access(sid_buffer, GRANT_ACCESS, mask)
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = advapi32.GetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result != 0:
            raise RunnerError(f"GetNamedSecurityInfoW({path}) failed: {ctypes.WinError(result).strerror}")
        new_acl = ctypes.c_void_p()
        try:
            result = advapi32.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new_acl))
            if result != 0:
                raise RunnerError(f"SetEntriesInAclW({path}) failed: {ctypes.WinError(result).strerror}")
            if not new_acl.value:
                raise RunnerError(f"SetEntriesInAclW({path}) returned a null ACL")
        finally:
            if descriptor:
                kernel32.LocalFree(descriptor)
        try:
            result = advapi32.SetNamedSecurityInfoW(
                str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, new_acl, None
            )
            if result != 0:
                raise RunnerError(f"SetNamedSecurityInfoW({path}) failed: {ctypes.WinError(result).strerror}")
        finally:
            if new_acl:
                kernel32.LocalFree(new_acl)

    def _revoke_dir_ace(path: Path, sid: bytes) -> None:
        sid_buffer = ctypes.create_string_buffer(sid, len(sid))
        entry = _explicit_access(sid_buffer, REVOKE_ACCESS, 0)
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = advapi32.GetNamedSecurityInfoW(
            str(path),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result != 0:
            raise RunnerError(f"GetNamedSecurityInfoW({path}) failed")
        new_acl = ctypes.c_void_p()
        try:
            result = advapi32.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(new_acl))
            if result != 0:
                raise RunnerError("SetEntriesInAclW revoke failed")
        finally:
            if descriptor:
                kernel32.LocalFree(descriptor)
        try:
            result = advapi32.SetNamedSecurityInfoW(
                str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None, new_acl, None
            )
            if result != 0:
                raise RunnerError("SetNamedSecurityInfoW revoke failed")
        finally:
            if new_acl:
                kernel32.LocalFree(new_acl)

    def _set_token_default_dacl_grant(token: wintypes.HANDLE, sid: bytes) -> None:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_DEFAULT_DACL, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise RunnerError("GetTokenInformation(TokenDefaultDacl) size query returned 0")
        buffer = ctypes.create_string_buffer(needed.value)
        _win_call(
            advapi32.GetTokenInformation(
                token, TOKEN_DEFAULT_DACL, buffer, needed.value, ctypes.byref(needed)
            ),
            "GetTokenInformation",
            "TokenDefaultDacl",
        )
        current_dacl = struct.unpack_from("<Q", buffer, 0)[0]
        if not current_dacl:
            raise RunnerError("the restricted token carries no default DACL to extend")

        sid_buffer = ctypes.create_string_buffer(sid, len(sid))
        entry = _explicit_access(sid_buffer, GRANT_ACCESS, FILE_ALL_ACCESS)
        new_acl = ctypes.c_void_p()
        result = advapi32.SetEntriesInAclW(
            1, ctypes.byref(entry), ctypes.c_void_p(current_dacl), ctypes.byref(new_acl)
        )
        if result != 0:
            raise RunnerError(f"SetEntriesInAclW(default DACL) failed: {ctypes.WinError(result).strerror}")
        if not new_acl.value:
            raise RunnerError("SetEntriesInAclW(default DACL) returned a null ACL")
        try:
            pointer = ctypes.c_void_p(new_acl.value)
            _win_call(
                advapi32.SetTokenInformation(
                    token, TOKEN_DEFAULT_DACL, ctypes.byref(pointer), ctypes.sizeof(pointer)
                ),
                "SetTokenInformation",
                "TokenDefaultDacl",
            )
        finally:
            kernel32.LocalFree(new_acl)

    def _create_restricted_token(current_token: wintypes.HANDLE, sids: list[bytes]) -> wintypes.HANDLE:
        buffers = [ctypes.create_string_buffer(sid, len(sid)) for sid in sids]
        array = (SID_AND_ATTRIBUTES * len(buffers))()
        for index, buffer in enumerate(buffers):
            array[index].Sid = ctypes.cast(buffer, ctypes.c_void_p)
            array[index].Attributes = 0
        out = wintypes.HANDLE()
        _win_call(
            advapi32.CreateRestrictedToken(
                current_token,
                RESTRICTED_TOKEN_FLAGS,
                0,
                None,
                0,
                None,
                len(buffers),
                array,
                ctypes.byref(out),
            ),
            "CreateRestrictedToken",
            f"restricting SIDs: {len(buffers)}",
        )
        if not out.value:
            raise RunnerError("CreateRestrictedToken returned a null token")
        return out

    def _build_token(
        current_token: wintypes.HANDLE, write_sid: bytes | None
    ) -> tuple[wintypes.HANDLE, bytes]:
        logon = _find_logon_sid(current_token)
        world = _make_world_sid()
        sids = [logon, world]
        if write_sid is not None:
            sids.append(write_sid)
        return _create_restricted_token(current_token, sids), world

    def _create_kill_on_close_job() -> wintypes.HANDLE:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise RunnerError(f"CreateJobObjectW failed: {ctypes.WinError(ctypes.get_last_error()).strerror}")
        info = ctypes.create_string_buffer(144)
        struct.pack_into("<I", info, 16, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        _win_call(
            kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, info, 144),
            "SetInformationJobObject",
            "KILL_ON_JOB_CLOSE",
        )
        return job

    def _handle_value(handle: wintypes.HANDLE) -> int:
        return ctypes.cast(handle, ctypes.c_void_p).value or 0

    def _inherit_standard_handles(
        si: STARTUPINFOW,
    ) -> list[tuple[wintypes.HANDLE, wintypes.DWORD]]:
        handles = (
            (STD_INPUT_HANDLE, "stdin"),
            (STD_OUTPUT_HANDLE, "stdout"),
            (STD_ERROR_HANDLE, "stderr"),
        )
        saved: list[tuple[wintypes.HANDLE, wintypes.DWORD]] = []
        standard: list[wintypes.HANDLE] = []
        for identifier, label in handles:
            handle = kernel32.GetStdHandle(identifier)
            value = _handle_value(handle)
            if not value or value == 0xFFFFFFFFFFFFFFFF:
                raise RunnerError(f"standard {label} is not available; refusing to start the child")
            standard.append(handle)
            flags = wintypes.DWORD(0)
            if not kernel32.GetHandleInformation(handle, ctypes.byref(flags)):
                raise RunnerError(
                    f"GetHandleInformation({label}) failed: "
                    f"{ctypes.WinError(ctypes.get_last_error()).strerror}"
                )
            saved.append((handle, flags.value))
            _win_call(
                kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT),
                "SetHandleInformation",
                label,
            )
        si.hStdInput = standard[0]
        si.hStdOutput = standard[1]
        si.hStdError = standard[2]
        return saved

    def _restore_standard_handles(saved: list[tuple[wintypes.HANDLE, wintypes.DWORD]]) -> None:
        for handle, flags in saved:
            kernel32.SetHandleInformation(handle, HANDLE_FLAG_INHERIT, flags & HANDLE_FLAG_INHERIT)

    def _spawn_restricted(token: wintypes.HANDLE, command_line: list[str], cwd: Path) -> int:
        job = _create_kill_on_close_job()
        process = wintypes.HANDLE()
        thread = wintypes.HANDLE()
        created = False
        completed = False
        saved_handles: list[tuple[wintypes.HANDLE, wintypes.DWORD]] = []
        try:
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(STARTUPINFOW)
            si.dwFlags = STARTF_USESTDHANDLES
            saved_handles = _inherit_standard_handles(si)
            pi = PROCESS_INFORMATION()
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(command_line))
            try:
                started = advapi32.CreateProcessAsUserW(
                    token,
                    None,
                    command,
                    None,
                    None,
                    True,
                    CREATE_SUSPENDED,
                    None,
                    str(cwd),
                    ctypes.byref(si),
                    ctypes.byref(pi),
                )
                create_error = None if started else ctypes.get_last_error()
            finally:
                _restore_standard_handles(saved_handles)
            if create_error is not None:
                raise RunnerError(f"CreateProcessAsUserW failed: {ctypes.WinError(create_error).strerror}")

            process = pi.hProcess
            thread = pi.hThread
            created = True
            _win_call(kernel32.AssignProcessToJobObject(job, process), "AssignProcessToJobObject")
            if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                raise RunnerError(f"ResumeThread failed: {ctypes.WinError(ctypes.get_last_error()).strerror}")
            wait_result = kernel32.WaitForSingleObject(process, INFINITE)
            if wait_result != WAIT_OBJECT_0:
                raise RunnerError(f"WaitForSingleObject returned {wait_result}")
            exit_code = wintypes.DWORD(0)
            _win_call(
                kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)),
                "GetExitCodeProcess",
            )
            completed = True
            return int(exit_code.value)
        finally:
            if created and not completed and process:
                kernel32.TerminateProcess(process, 1)
            if thread:
                kernel32.CloseHandle(thread)
            if process:
                kernel32.CloseHandle(process)
            if job:
                kernel32.CloseHandle(job)

    def _run_windows(workspace: Path, temp: Path, mode: str, child: list[str]) -> int:
        _check_structure_sizes()
        current = wintypes.HANDLE()
        restricted = wintypes.HANDLE()
        temp_sid: bytes | None = None
        temp_granted = False
        saved_environment: dict[str, str | None] = {}
        try:
            current = _open_current_token()
            if mode == "workspace-write":
                sid_text = workspace_write_sid(workspace)
                temp_sid = _sid_from_string(sid_text)
                # Standing grant: the workspace stays writable for later calls
                # under the same synthetic SID.
                _grant_dir_ace(workspace, temp_sid)
                # Private temp grant: revoked in the finally block.
                _grant_dir_ace(temp, temp_sid)
                temp_granted = True
                restricted, _ = _build_token(current, temp_sid)
                _set_token_default_dacl_grant(restricted, temp_sid)
            else:
                restricted, world = _build_token(current, None)
                _set_token_default_dacl_grant(restricted, world)

            for key in ("TEMP", "TMP"):
                saved_environment[key] = os.environ.get(key)
                os.environ[key] = str(temp)
            return _spawn_restricted(restricted, child, Path.cwd())
        finally:
            for key, value in saved_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if temp_granted and temp_sid is not None:
                # cleanup is best-effort
                # The child already ran; a cleanup failure must not turn a
                # successful command into a failure. The temp dir is private.
                with contextlib.suppress(Exception):
                    _revoke_dir_ace(temp, temp_sid)
            for handle in (restricted, current):
                if handle and handle.value:
                    kernel32.CloseHandle(handle)


if __name__ == "__main__":
    sys.exit(main())
