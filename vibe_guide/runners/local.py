"""Cross-process-safe local runner for explicitly confirmed commands."""

import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
import uuid

from ..authorization import validate_runtime_contract
from ..adapters.task_provider import require_complex_monitor_dispatch
from ..contracts import RunEvent, RunHandle, Runner
from .._wincompat import (
    IS_WINDOWS as _IS_WINDOWS,
    process_start_token as _process_start_token,
    spawn_worker,
)


_RESULT_WAIT_SECONDS = 1.0


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("local runner metadata path may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("local runner metadata must be a regular file")
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("local runner metadata exceeds the size bound")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("local runner metadata is invalid") from error
    if not isinstance(value, dict):
        raise ValueError("local runner metadata must be an object")
    return value


# _process_start_token is imported from .._wincompat (cross-platform).


class LocalRunner(Runner):
    """Run a confirmed command through a durable bounded worker process."""

    def __init__(
        self,
        confirmed_commands: Mapping[str, Sequence[str]],
        roots: Optional[Sequence[Path]] = None,
    ):
        if not isinstance(confirmed_commands, Mapping) or not confirmed_commands:
            raise ValueError("at least one confirmed adapter command is required")
        self._confirmed: Dict[str, Tuple[str, ...]] = {}
        for adapter_id, command in confirmed_commands.items():
            self._confirmed[self._adapter_id(adapter_id)] = self._command(command)
        self._roots = [Path(root).resolve() for root in (roots or ())]
        self._metadata_paths: Dict[str, Path] = {}
        self._processes: Dict[str, int] = {}
        # Windows: cache the Popen handle so stop/poll manage the child directly
        # instead of POSIX process-group signals.  POSIX leaves this empty.
        self._popens: Dict[str, Any] = {}
        self.start_contracts = []

    @property
    def start_count(self) -> int:
        return len(self.start_contracts)

    @staticmethod
    def _adapter_id(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("adapter_id is required")
        normalized = value.strip()
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
        if any(character not in allowed for character in normalized):
            raise ValueError("adapter_id must be a simple identifier")
        return normalized

    @staticmethod
    def _command(value: Any) -> Tuple[str, ...]:
        if not isinstance(value, (list, tuple)) or not value or len(value) > 64:
            raise ValueError("adapter command must be a non-empty bounded list")
        if any(
            not isinstance(item, str) or not item or "\x00" in item
            for item in value
        ):
            raise ValueError("adapter command contains an invalid argument")
        return tuple(value)

    @staticmethod
    def _metadata_dir(worktree: Path) -> Path:
        vibe = worktree / ".vibe"
        directory = vibe / "local-runner"
        if vibe.is_symlink() or directory.is_symlink():
            raise ValueError("runner metadata directory may not be a symlink")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def start(self, contract: dict, worktree: Path) -> RunHandle:
        # Guard before command validation, process creation or metadata writes.
        require_complex_monitor_dispatch(contract, self._project_paths(contract, worktree))
        contract = validate_runtime_contract(contract)
        adapter_id = self._adapter_id(contract.get("adapter_id"))
        command = self._command(contract.get("command"))
        if self._confirmed.get(adapter_id) != command:
            raise PermissionError("command is not the exact confirmed adapter command")
        worktree = Path(worktree).resolve()
        if not worktree.is_dir():
            raise ValueError("runner worktree must be an existing directory")
        if worktree not in self._roots:
            self._roots.append(worktree)

        handle = RunHandle("local-" + uuid.uuid4().hex)
        directory = self._metadata_dir(worktree)
        metadata_path = directory / (handle.run_id + ".json")
        result_path = directory / (handle.run_id + ".result.json")
        provenance = {
            "node_id": contract.get("node_id"),
            "role": contract.get("role"),
            "task_id": contract.get("task_id"),
            "generation": contract.get("generation"),
            "handle_id": handle.run_id,
        }
        owner_digest = _digest(
            {"handle_id": handle.run_id, "owner": uuid.uuid4().hex}
        )
        environment = os.environ.copy()
        environment.update(
            {
                "VIBE_LOCAL_COMMAND": base64.b64encode(
                    json.dumps(list(command)).encode("utf-8")
                ).decode("ascii"),
                "VIBE_LOCAL_PROVENANCE": base64.b64encode(
                    json.dumps(provenance).encode("utf-8")
                ).decode("ascii"),
                "VIBE_LOCAL_RESULT": str(result_path),
                "VIBE_LOCAL_OWNER_DIGEST": owner_digest,
                "VIBE_LOCAL_WORKTREE": str(worktree),
                "VIBE_NODE_ID": str(contract.get("node_id", "")),
                "VIBE_TASK_ROLE": str(contract.get("role", "")),
                "VIBE_TASK_PHASE": str(contract.get("phase", "")),
            }
        )
        package_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = os.pathsep.join(
            item
            for item in (package_root, environment.get("PYTHONPATH", ""))
            if item
        )
        worker_command = [
            sys.executable,
            "-m",
            "vibe_guide.runners.local_worker",
        ]
        pid, proc = spawn_worker(worker_command, environment)
        if proc is not None:
            # Windows only: keep the Popen handle so stop/poll manage the
            # child directly.  POSIX spawn_worker returns None.
            self._popens[handle.run_id] = proc
        token = None
        for _ in range(20):
            token = _process_start_token(pid)
            if token:
                break
            if result_path.exists():
                break
            time.sleep(0.01)
        if token is None and not result_path.exists():
            if proc is not None:
                try:
                    proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                except OSError:
                    pass
                self._popens.pop(handle.run_id, None)
            else:
                os.killpg(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            raise RuntimeError("local runner process identity cannot be recorded")
        metadata = {
            "schema_version": 2,
            "handle_id": handle.run_id,
            "pid": pid,
            "process_identity": token,
            "status": "running",
            "exit_status": None,
            "adapter_id": adapter_id,
            "command_name": Path(command[0]).name,
            "command_digest": _digest(list(command)),
            "provenance_ref": "confirmed-command:" + adapter_id,
            "owner_digest": owner_digest,
            "event_count": 0,
            "output_truncated": False,
        }
        _atomic_json(metadata_path, metadata)
        self._metadata_paths[handle.run_id] = metadata_path
        self._processes[handle.run_id] = pid
        self.start_contracts.append(
            {
                "adapter_id": adapter_id,
                "node_id": contract.get("node_id"),
                "role": contract.get("role"),
                "phase": contract.get("phase"),
                "task_id": contract.get("task_id"),
                "generation": contract.get("generation"),
            }
        )
        return handle

    @staticmethod
    def _project_paths(contract: Mapping[str, Any], worktree: Path):
        """Resolve project paths only when a complex guard needs them."""
        if not isinstance(contract, Mapping):
            return None
        raw = contract.get("project_root") or contract.get("paths_root")
        if raw:
            from ..paths import ProjectPaths
            return ProjectPaths(Path(str(raw)).resolve())
        candidate = Path(worktree).resolve()
        for parent in (candidate, *candidate.parents):
            if (parent / ".vibe").is_dir():
                from ..paths import ProjectPaths
                return ProjectPaths(parent)
        return None

    def _locate_metadata(self, handle: RunHandle) -> Path:
        if not isinstance(handle, RunHandle):
            raise ValueError("invalid local runner handle")
        known = self._metadata_paths.get(handle.run_id)
        if known is not None:
            return known
        matches = []
        for root in self._roots:
            candidate = root / ".vibe/local-runner" / (handle.run_id + ".json")
            if candidate.exists() or candidate.is_symlink():
                matches.append(candidate)
        if len(matches) != 1:
            raise ValueError("unknown local runner handle")
        self._metadata_paths[handle.run_id] = matches[0]
        return matches[0]

    def _validated_metadata(self, handle: RunHandle) -> Tuple[Path, Dict[str, Any]]:
        path = self._locate_metadata(handle)
        metadata = _read_json(path)
        required = {
            "schema_version",
            "handle_id",
            "pid",
            "process_identity",
            "status",
            "exit_status",
            "adapter_id",
            "command_name",
            "command_digest",
            "provenance_ref",
            "owner_digest",
            "event_count",
            "output_truncated",
        }
        if set(metadata) != required or metadata["schema_version"] != 2:
            raise ValueError("local runner metadata schema is invalid")
        adapter_id = metadata["adapter_id"]
        if (
            metadata["handle_id"] != handle.run_id
            or adapter_id not in self._confirmed
            or metadata["command_digest"] != _digest(list(self._confirmed[adapter_id]))
        ):
            raise ValueError("local runner metadata ownership is invalid")
        return path, metadata

    def poll(self, handle: RunHandle):
        metadata_path, metadata = self._validated_metadata(handle)
        result_path = metadata_path.with_name(handle.run_id + ".result.json")
        deadline = time.monotonic() + _RESULT_WAIT_SECONDS
        while not result_path.exists():
            current_token = _process_start_token(int(metadata["pid"]))
            if current_token == metadata["process_identity"] and current_token:
                return []
            if time.monotonic() >= deadline:
                raise ValueError("local runner reattachment cannot be proven")
            time.sleep(0.01)
        result = _read_json(result_path)
        if handle.run_id in self._processes:
            try:
                if _IS_WINDOWS:
                    popen = self._popens.get(handle.run_id)
                    if popen is not None:
                        popen.poll()
                else:
                    os.waitpid(self._processes[handle.run_id], os.WNOHANG)
            except ChildProcessError:
                pass
        if (
            set(result)
            != {
                "schema_version",
                "handle_id",
                "owner_digest",
                "exit_status",
                "output_truncated",
                "events",
            }
            or result["schema_version"] != 1
            or result["handle_id"] != handle.run_id
            or result["owner_digest"] != metadata["owner_digest"]
            or not isinstance(result["events"], list)
        ):
            raise ValueError("local runner result ownership is invalid")
        events = []
        for item in result["events"]:
            if not isinstance(item, dict) or set(item) != {"event", "data"}:
                raise ValueError("local runner result event is invalid")
            events.append(RunEvent(item["event"], item["data"]))
        metadata["status"] = "exited"
        metadata["exit_status"] = result["exit_status"]
        metadata["event_count"] = len(events)
        metadata["output_truncated"] = result["output_truncated"]
        _atomic_json(metadata_path, metadata)
        self._popens.pop(handle.run_id, None)
        return events

    def stop(self, handle: RunHandle) -> None:
        metadata_path, metadata = self._validated_metadata(handle)
        result_path = metadata_path.with_name(handle.run_id + ".result.json")
        pid = int(metadata["pid"])
        current_token = _process_start_token(pid)
        if metadata["status"] == "exited" and result_path.exists():
            return
        if not current_token or current_token != metadata["process_identity"]:
            raise ValueError("local runner process identity cannot be proven")
        process_pid = self._processes.get(handle.run_id)
        if _IS_WINDOWS:
            popen = self._popens.pop(handle.run_id, None)
            if popen is not None:
                try:
                    popen.terminate()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    popen.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        popen.kill()
                    except OSError:
                        pass
                except OSError:
                    pass
            else:
                # A runner recovered from a snapshot has neither a Popen
                # handle nor a _processes entry (both are in-memory caches).
                # Signal the identity-verified metadata pid directly; on
                # Windows os.kill(pid, SIGTERM) maps to TerminateProcess.
                # Never skip signalling: writing the stopped result below
                # while the worker still runs would forge completion
                # evidence.
                try:
                    os.kill(
                        process_pid if process_pid is not None else pid,
                        signal.SIGTERM,
                    )
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                if process_pid is not None:
                    try:
                        os.kill(process_pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
            if process_pid is not None:
                try:
                    os.waitpid(process_pid, 0)
                except ChildProcessError:
                    pass
        if not result_path.exists():
            _atomic_json(
                result_path,
                {
                    "schema_version": 1,
                    "handle_id": handle.run_id,
                    "owner_digest": metadata["owner_digest"],
                    "exit_status": None,
                    "output_truncated": False,
                    "events": [
                        {
                            "event": "stopped",
                            "data": {
                                "reason": "local process was stopped",
                                "handle_id": handle.run_id,
                            },
                        }
                    ],
                },
            )
