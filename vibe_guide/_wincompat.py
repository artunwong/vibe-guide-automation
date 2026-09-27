"""Cross-platform shims so the package imports and runs on Windows.

POSIX paths keep their original fcntl/posix_spawn/killpg semantics untouched;
Windows uses stdlib-only replacements (msvcrt byte-range locks, ctypes process
identity, subprocess.Popen).  No third-party packages are required.

Design notes:
- ``flock_exclusive`` / ``flock_release`` emulate ``fcntl.flock``.  On Windows
  they lock byte 0 of the fd (msvcrt.locking).  For the non-blocking variant a
  failed acquisition raises ``BlockingIOError`` so existing POSIX call sites
  that catch ``BlockingIOError`` and poll keep working unchanged.
- ``process_start_token`` returns a stable process-creation fingerprint used to
  detect PID reuse.  POSIX keeps the ``ps -o lstart=`` probe; Windows queries
  ``GetProcessTimes`` via ctypes and returns the creation FILETIME.
- ``spawn_worker`` starts the local runner worker.  POSIX keeps
  ``os.posix_spawn`` with ``setpgroup=0``; Windows uses ``subprocess.Popen``
  and also returns the ``Popen`` handle so the runner can manage it directly.
"""

from __future__ import annotations

import errno
import os
import sys
import time
from typing import Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")


# --------------------------------------------------------------------------- #
# File lock shim (replaces fcntl.flock)                                       #
# --------------------------------------------------------------------------- #
if IS_WINDOWS:
    import msvcrt

    def flock_exclusive(fd: int, *, blocking: bool = True, timeout: float = 10.0) -> None:
        """Acquire an exclusive advisory lock on byte 0 of ``fd``.

        ``blocking=True`` polls until acquired (emulating ``flock(LOCK_EX)``).
        ``blocking=False`` performs a single non-blocking attempt and raises
        ``BlockingIOError`` on contention so callers' own polling loops keep
        working unchanged.  ``timeout`` is accepted for API symmetry only; the
        non-blocking path never waits.
        """
        os.lseek(fd, 0, os.SEEK_SET)
        if blocking:
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    time.sleep(0.01)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError(
                errno.EAGAIN, "unable to acquire lock", None
            ) from error

    def flock_release(fd: int) -> None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:
    import fcntl  # type: ignore[import-not-found]

    def flock_exclusive(fd: int, *, blocking: bool = True, timeout: float = 10.0) -> None:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fd, flags)

    def flock_release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Process start token (replaces `ps -o lstart=`)                              #
# --------------------------------------------------------------------------- #
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class _FILETIME(ctypes.Structure):
        _fields_ = [
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        ]

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
        ctypes.POINTER(_FILETIME),
    ]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def process_start_token(pid: int) -> Optional[str]:
        """Return a Windows process-creation fingerprint, or ``None``."""
        handle = _kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return None
        try:
            creation = _FILETIME()
            exit_time = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return "{:08x}{:08x}".format(
                creation.dwHighDateTime, creation.dwLowDateTime
            )
        finally:
            _kernel32.CloseHandle(handle)

else:
    import subprocess

    def process_start_token(pid: int) -> Optional[str]:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            text=True,
            capture_output=True,
            check=False,
        )
        value = " ".join(result.stdout.split())
        return value or None


# --------------------------------------------------------------------------- #
# Worker spawn (replaces os.posix_spawn + setpgroup)                          #
# --------------------------------------------------------------------------- #
if IS_WINDOWS:
    import subprocess

    def spawn_worker(
        command, env, cwd: Optional[str] = None
    ) -> Tuple[int, "subprocess.Popen"]:
        """Spawn the local runner worker on Windows.

        Returns ``(pid, popen)`` so the caller can manage the process via the
        ``Popen`` handle (terminate/wait) instead of POSIX process-group
        signals.
        """
        proc = subprocess.Popen(
            list(command),
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Windows has no process groups; start_new_session must stay False
            # so the child remains controllable as a plain subprocess.
            start_new_session=False,
        )
        return proc.pid, proc

else:

    def spawn_worker(command, env, cwd: Optional[str] = None):
        """Spawn the local runner worker on POSIX (unchanged semantics)."""
        pid = os.posix_spawn(command[0], list(command), env, setpgroup=0)
        return pid, None
