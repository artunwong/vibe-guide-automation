"""Contract tests for vibe_guide._wincompat and its call sites.

Two layers:
- POSIX equivalence: on this platform the shims must delegate to the original
  fcntl / posix_spawn / `ps` semantics so macOS/Linux behavior is unchanged.
- Windows branch: the module source is executed fresh against faked
  win32 modules (msvcrt / ctypes / subprocess) so the Windows implementations
  are exercised without a Windows machine.  The ``append_event`` no-dir_fd
  fallback and the recovered-runner ``stop()`` signal fallback are likewise
  exercised on POSIX via targeted patches.
"""

import errno
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import _wincompat
from vibe_guide.contracts import RunEvent
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import append_event


# --------------------------------------------------------------------------- #
# POSIX equivalence                                                            #
# --------------------------------------------------------------------------- #
@unittest.skipIf(_wincompat.IS_WINDOWS, "POSIX shim contract")
class PosixShimEquivalenceTests(unittest.TestCase):
    def test_flock_exclusive_delegates_to_fcntl(self):
        import fcntl

        calls = []
        with mock.patch.object(
            _wincompat.fcntl, "flock", side_effect=lambda fd, flags: calls.append((fd, flags))
        ):
            _wincompat.flock_exclusive(7)
            _wincompat.flock_exclusive(8, blocking=False)
        self.assertEqual(
            calls, [(7, fcntl.LOCK_EX), (8, fcntl.LOCK_EX | fcntl.LOCK_NB)]
        )

    def test_flock_release_delegates_to_fcntl(self):
        import fcntl

        calls = []
        with mock.patch.object(
            _wincompat.fcntl, "flock", side_effect=lambda fd, flags: calls.append((fd, flags))
        ):
            _wincompat.flock_release(9)
        self.assertEqual(calls, [(9, fcntl.LOCK_UN)])

    def test_flock_contention_raises_blocking_io_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock"
            path.touch()
            fd1 = os.open(str(path), os.O_RDWR)
            fd2 = os.open(str(path), os.O_RDWR)
            try:
                _wincompat.flock_exclusive(fd1)
                with self.assertRaises(BlockingIOError):
                    _wincompat.flock_exclusive(fd2, blocking=False)
                _wincompat.flock_release(fd1)
                _wincompat.flock_exclusive(fd2, blocking=False)
            finally:
                os.close(fd1)
                os.close(fd2)

    def test_spawn_worker_delegates_to_posix_spawn(self):
        with mock.patch.object(os, "posix_spawn", return_value=4242) as spawn:
            pid, proc = _wincompat.spawn_worker(["/bin/echo", "ok"], {"A": "B"})
        self.assertEqual((pid, proc), (4242, None))
        spawn.assert_called_once_with(
            "/bin/echo", ["/bin/echo", "ok"], {"A": "B"}, setpgroup=0
        )

    def test_process_start_token_uses_ps_probe(self):
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=" Sat Sep 27 10:00:00 2026 \n", stderr=""
        )
        with mock.patch.object(_wincompat.subprocess, "run", return_value=fake) as run:
            token = _wincompat.process_start_token(123)
        self.assertEqual(token, "Sat Sep 27 10:00:00 2026")
        self.assertEqual(run.call_args[0][0], ["ps", "-o", "lstart=", "-p", "123"])


# --------------------------------------------------------------------------- #
# Windows branch (faked win32 environment)                                     #
# --------------------------------------------------------------------------- #
def _load_windows_wincompat(msvcrt=None, ctypes_module=None, subprocess_module=None):
    """Execute _wincompat.py fresh with sys.platform=win32 and fake modules."""
    # The token section touches ctypes.windll at module level, so every
    # Windows-branch load needs the fake even when the test targets locks.
    if ctypes_module is None:
        ctypes_module = _fake_ctypes()
    module_path = Path(_wincompat.__file__)
    spec = importlib.util.spec_from_file_location("_wincompat_windows_test", module_path)
    module = importlib.util.module_from_spec(spec)
    fakes = {"msvcrt": msvcrt, "ctypes": ctypes_module, "subprocess": subprocess_module}
    if ctypes_module is not None:
        fakes["ctypes.wintypes"] = ctypes_module.wintypes
    saved_platform = sys.platform
    saved_modules = {}
    sys.platform = "win32"
    try:
        for name, fake in fakes.items():
            if fake is None:
                continue
            saved_modules[name] = sys.modules.get(name)
            sys.modules[name] = fake
        spec.loader.exec_module(module)
    finally:
        sys.platform = saved_platform
        for name, previous in saved_modules.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


def _fake_msvcrt(fail_times=0):
    module = types.ModuleType("msvcrt")
    module.LK_NBLCK = 1
    module.LK_UNLCK = 2
    state = {"calls": [], "fail_times": fail_times}

    def locking(fd, mode, nbytes):
        state["calls"].append((fd, mode, nbytes))
        if state["fail_times"]:
            state["fail_times"] -= 1
            raise OSError(13, "lock violation")

    module.locking = locking
    module._state = state
    return module


def _fake_subprocess(popen_pid=4321):
    module = types.ModuleType("subprocess")
    module.DEVNULL = object()
    state = {"calls": []}

    class Popen:
        def __init__(self, command, **kwargs):
            state["calls"].append((command, kwargs))
            self.pid = popen_pid

    module.Popen = Popen
    module._state = state
    return module


def _fake_ctypes(creation=(0x00000001, 0x00000002), open_ok=True):
    import ctypes as real_ctypes
    from ctypes import wintypes as real_wintypes

    module = types.ModuleType("ctypes")
    module.Structure = real_ctypes.Structure
    module.POINTER = real_ctypes.POINTER
    module.byref = real_ctypes.byref
    module.wintypes = real_wintypes
    state = {"opened": [], "closed": []}
    kernel32 = types.SimpleNamespace()

    def OpenProcess(access, inherit, pid):
        state["opened"].append(pid)
        return 99 if open_ok else 0

    def GetProcessTimes(handle, creation_ptr, _exit, _kernel, _user):
        creation_ptr._obj.dwHighDateTime = creation[0]
        creation_ptr._obj.dwLowDateTime = creation[1]
        return True

    def CloseHandle(handle):
        state["closed"].append(handle)
        return True

    kernel32.OpenProcess = OpenProcess
    kernel32.GetProcessTimes = GetProcessTimes
    kernel32.CloseHandle = CloseHandle
    module.windll = types.SimpleNamespace(kernel32=kernel32)
    module._state = state
    return module


class WindowsShimTests(unittest.TestCase):
    def setUp(self):
        # The lock shims lseek the fd, so tests need a real open file.
        self.temporary = tempfile.TemporaryDirectory()
        self.lock_file = Path(self.temporary.name) / "lock"
        self.lock_file.touch()
        self.fd = os.open(str(self.lock_file), os.O_RDWR)
        self.addCleanup(os.close, self.fd)
        self.addCleanup(self.temporary.cleanup)

    def test_flock_non_blocking_contention_raises_blocking_io_error(self):
        msvcrt = _fake_msvcrt(fail_times=1)
        module = _load_windows_wincompat(msvcrt=msvcrt)
        self.assertTrue(module.IS_WINDOWS)
        with self.assertRaises(BlockingIOError) as caught:
            module.flock_exclusive(self.fd, blocking=False)
        self.assertEqual(caught.exception.errno, errno.EAGAIN)

    def test_flock_blocking_retries_until_acquired(self):
        msvcrt = _fake_msvcrt(fail_times=2)
        module = _load_windows_wincompat(msvcrt=msvcrt)
        module.flock_exclusive(self.fd)
        modes = [call[1] for call in msvcrt._state["calls"]]
        self.assertEqual(modes, [msvcrt.LK_NBLCK] * 3)

    def test_flock_release_unlocks(self):
        msvcrt = _fake_msvcrt()
        module = _load_windows_wincompat(msvcrt=msvcrt)
        module.flock_release(self.fd)
        self.assertEqual(msvcrt._state["calls"], [(self.fd, msvcrt.LK_UNLCK, 1)])

    def test_spawn_worker_returns_popen_handle(self):
        subprocess_fake = _fake_subprocess(popen_pid=4321)
        module = _load_windows_wincompat(
            msvcrt=_fake_msvcrt(),
            ctypes_module=_fake_ctypes(),
            subprocess_module=subprocess_fake,
        )
        pid, proc = module.spawn_worker(["python", "-m", "worker"], {"A": "B"})
        self.assertEqual(pid, 4321)
        self.assertIsInstance(proc, subprocess_fake.Popen)
        command, kwargs = subprocess_fake._state["calls"][0]
        self.assertEqual(command, ["python", "-m", "worker"])
        self.assertFalse(kwargs["start_new_session"])

    def test_process_start_token_from_get_process_times(self):
        ctypes_fake = _fake_ctypes(creation=(0x00000001, 0x00000002))
        module = _load_windows_wincompat(
            msvcrt=_fake_msvcrt(), ctypes_module=ctypes_fake
        )
        self.assertEqual(module.process_start_token(1234), "0000000100000002")
        self.assertEqual(ctypes_fake._state["opened"], [1234])
        self.assertEqual(ctypes_fake._state["closed"], [99])

    def test_process_start_token_returns_none_for_dead_pid(self):
        ctypes_fake = _fake_ctypes(open_ok=False)
        module = _load_windows_wincompat(
            msvcrt=_fake_msvcrt(), ctypes_module=ctypes_fake
        )
        self.assertIsNone(module.process_start_token(1234))


# --------------------------------------------------------------------------- #
# append_event no-dir_fd fallback (Windows runtime path)                       #
# --------------------------------------------------------------------------- #
class AppendEventFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_append_event_without_dir_fd_support(self):
        used_dir_fd = []
        real_open = os.open

        def tracking_open(path, flags, mode=0o777, **kwargs):
            used_dir_fd.append("dir_fd" in kwargs)
            return real_open(path, flags, mode, **kwargs)

        # Simulate Windows: os.open reports no dir_fd support.
        with mock.patch.object(os, "supports_dir_fd", set()), mock.patch.object(
            os, "open", tracking_open
        ):
            sequence = append_event(
                self.paths, RunEvent("started", {"run_id": "run-fallback"})
            )
        self.assertEqual(sequence, 1)
        self.assertTrue(used_dir_fd, "append_event did not open any file")
        self.assertFalse(
            any(used_dir_fd), "dir_fd must not be used when unsupported"
        )
        event_path = (
            Path(self.temporary.name) / ".vibe/runs/run-fallback/events.jsonl"
        )
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        self.assertEqual([record["event"] for record in records], ["started"])

    def test_append_event_fallback_rejects_symlink_log(self):
        run_dir = Path(self.temporary.name) / ".vibe/runs/run-symlink"
        run_dir.mkdir(parents=True)
        target = Path(self.temporary.name) / "elsewhere.jsonl"
        target.write_text("", encoding="utf-8")
        (run_dir / "events.jsonl").symlink_to(target)
        with mock.patch.object(os, "supports_dir_fd", set()):
            with self.assertRaisesRegex(ValueError, "symlink"):
                append_event(
                    self.paths, RunEvent("started", {"run_id": "run-symlink"})
                )


# --------------------------------------------------------------------------- #
# LocalRunner.stop() after monitor recovery (no in-memory caches)              #
# --------------------------------------------------------------------------- #
@unittest.skipIf(_wincompat.IS_WINDOWS, "requires POSIX signal semantics")
class LocalRunnerRecoveredStopTests(unittest.TestCase):
    def test_stop_after_recovery_signals_metadata_pid(self):
        import vibe_guide.runners.local as local_module
        from vibe_guide.runners.local import LocalRunner

        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        runner = LocalRunner(confirmed_commands={"fixture-agent": command})
        handle = runner.start(
            {
                "adapter_id": "fixture-agent",
                "command": command,
                "node_id": "recovered-stop",
                "role": "developer",
                "task_id": "developer:recovered-stop",
                "generation": 1,
            },
            Path(root.name),
        )
        metadata_path = (
            Path(root.name) / ".vibe/local-runner" / (handle.run_id + ".json")
        )
        result_path = metadata_path.with_name(handle.run_id + ".result.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        pid = int(metadata["pid"])
        self.addCleanup(self._reap, pid)

        # A monitor restored from a snapshot builds a fresh runner: both the
        # Popen handle and the _processes cache are empty.  stop() must still
        # signal the identity-verified metadata pid instead of silently
        # recording a stop while the worker keeps running.
        recovered = LocalRunner(confirmed_commands={"fixture-agent": command}, roots=[Path(root.name)])
        with mock.patch.object(local_module, "_IS_WINDOWS", True):
            recovered.stop(handle)

        self.assertTrue(result_path.exists())
        reaped = False
        for _ in range(100):
            done, _status = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                reaped = True
                break
            time.sleep(0.05)
        self.assertTrue(reaped, "worker process was not terminated by stop()")

    @staticmethod
    def _reap(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, OSError):
            pass


if __name__ == "__main__":
    unittest.main()
