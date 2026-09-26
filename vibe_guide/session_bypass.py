"""Session-bound, one-time Vibe wizard bypasses.

The challenge is returned to the caller once, but only its digest is ever
written to disk or emitted in durable events.  A granted bypass remains valid
for the entry session until expiry; the challenge itself cannot be replayed.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from ._wincompat import flock_exclusive, flock_release
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any, Dict, Optional, Tuple


_COMMAND = re.compile(r"^BYPASS VIBE ([A-Za-z0-9_-]+)$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SCOPE = "wizard"
_TTL = timedelta(minutes=15)
_STORE = "session-bypass.json"
_EVENTS = "session-events.jsonl"


class BypassError(PermissionError):
    """A malformed, expired, replayed, or incorrectly scoped bypass."""


def _clock(value: Optional[datetime]) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _clock(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp is required")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return _clock(datetime.fromisoformat(normalized))
    except (TypeError, ValueError) as error:
        raise ValueError("timestamp is invalid") from error


def _validate_session(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION.fullmatch(session_id):
        raise ValueError("session id is invalid")
    return session_id


def _digest(challenge: str) -> str:
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()


def _event_identity(event: Dict[str, Any]) -> str:
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _digest(encoded)


@dataclass(frozen=True)
class ChallengeRecord:
    session_id: str
    challenge_digest: str
    created_at: str
    expires_at: str
    scope: str = _SCOPE
    reason: str = ""
    consumed: bool = False
    session_ended: bool = False
    # The raw challenge is intentionally transient and excluded from to_dict.
    challenge: Optional[str] = None
    # Events are staged here until the durable event log acknowledges them.
    pending_events: Tuple[Dict[str, Any], ...] = ()

    @property
    def used(self) -> bool:
        return self.consumed

    @property
    def expired(self) -> bool:
        return _clock() >= _parse_timestamp(self.expires_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "challenge_digest": self.challenge_digest,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "reason": self.reason,
            "consumed": self.consumed,
            "session_ended": self.session_ended,
            "pending_events": [dict(event) for event in self.pending_events],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChallengeRecord":
        if not isinstance(data, dict):
            raise ValueError("challenge record is invalid")
        required = {
            "session_id", "challenge_digest", "created_at", "expires_at",
            "scope", "reason", "consumed", "session_ended",
        }
        if set(data) - required - {"pending_events"} or not required.issubset(data):
            raise ValueError("challenge record schema is invalid")
        session_id = _validate_session(data["session_id"])
        digest = data["challenge_digest"]
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("challenge digest is invalid")
        created = _parse_timestamp(data["created_at"])
        expires = _parse_timestamp(data["expires_at"])
        if expires <= created or expires - created > _TTL:
            raise ValueError("challenge expiry is invalid")
        if data["scope"] != _SCOPE or not isinstance(data["reason"], str):
            raise ValueError("challenge scope or reason is invalid")
        if not isinstance(data["consumed"], bool) or not isinstance(data["session_ended"], bool):
            raise ValueError("challenge state is invalid")
        pending_data = data.get("pending_events", [])
        if not isinstance(pending_data, list):
            raise ValueError("pending events are invalid")
        pending_events = []
        for event in pending_data:
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise ValueError("pending events are invalid")
            if not isinstance(event.get("data"), dict):
                raise ValueError("pending events are invalid")
            pending_events.append(dict(event))
        return cls(
            session_id=session_id,
            challenge_digest=digest,
            created_at=_timestamp(created),
            expires_at=_timestamp(expires),
            scope=_SCOPE,
            reason=data["reason"],
            consumed=data["consumed"],
            session_ended=data["session_ended"],
            pending_events=tuple(pending_events),
            challenge=None,
        )


@dataclass(frozen=True)
class BypassResult:
    granted: bool
    record: ChallengeRecord
    events: Tuple[Dict[str, Any], ...] = ()

    @property
    def bypassed(self) -> bool:
        return self.granted


def create_challenge(session_id: str, now: datetime) -> ChallengeRecord:
    session_id = _validate_session(session_id)
    current = _clock(now)
    challenge = secrets.token_urlsafe(24)
    if not _COMMAND.fullmatch("BYPASS VIBE " + challenge):
        raise ValueError("challenge generator returned an invalid token")
    return ChallengeRecord(
        session_id=session_id,
        challenge_digest=_digest(challenge),
        created_at=_timestamp(current),
        expires_at=_timestamp(current + _TTL),
        challenge=challenge,
    )


def is_bypass_valid(record: ChallengeRecord, session_id: str, now: Optional[datetime] = None) -> bool:
    if not isinstance(record, ChallengeRecord):
        return False
    try:
        current = _clock(now)
        _validate_session(session_id)
        return (
            record.scope == _SCOPE
            and record.session_id == session_id
            and record.consumed
            and not record.session_ended
            and current < _parse_timestamp(record.expires_at)
        )
    except (TypeError, ValueError):
        return False


def grant_bypass(
    record: ChallengeRecord,
    command: str,
    reason: str,
    now: datetime,
) -> BypassResult:
    if not isinstance(record, ChallengeRecord):
        raise TypeError("record must be a ChallengeRecord")
    current = _clock(now)
    if record.consumed:
        raise BypassError("challenge already used")
    if record.session_ended or current >= _parse_timestamp(record.expires_at):
        raise BypassError("challenge expired")
    if not isinstance(command, str):
        raise BypassError("challenge command is invalid")
    match = _COMMAND.fullmatch(command)
    if not match or not secrets.compare_digest(_digest(match.group(1)), record.challenge_digest):
        raise BypassError("challenge command is invalid")
    if not isinstance(reason, str) or not reason.strip():
        raise BypassError("bypass reason is required")
    updated = replace(record, consumed=True, challenge=None, reason=reason.strip())
    event_data = {
        "actor": "user_entry",
        "session_id": record.session_id,
        "scope": record.scope,
        "reason": updated.reason,
        "expiry": updated.expires_at,
        "previous_state": "challenge_issued",
    }
    events = tuple(
        {"event": event_name, "data": dict(event_data)}
        for event_name in ("session_bypass_granted", "wizard_bypassed")
    )
    return BypassResult(True, updated, events)


def _store_path(paths: Any) -> Path:
    directory = paths.vibe
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _STORE
    if path.is_symlink():
        raise BypassError("challenge store may not be a symlink")
    return path


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".session-bypass.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_store(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise BypassError("challenge store is invalid") from error
    if not isinstance(data, dict):
        raise BypassError("challenge store is invalid")
    return data


@contextmanager
def _store_lock(paths: Any):
    path = _store_path(paths)
    lock_path = path.parent / ".session-bypass.lock"
    with open(lock_path, "a+", encoding="utf-8") as handle:
        flock_exclusive(handle.fileno())
        try:
            yield path
        finally:
            flock_release(handle.fileno())


def _save_record_unlocked(
    path: Path,
    record: ChallengeRecord,
    *,
    allow_pending_clear: bool = False,
) -> None:
    data = _read_store(path)
    existing_data = data.get(record.session_id)
    if existing_data is not None:
        existing = ChallengeRecord.from_dict(existing_data)
        same_challenge = existing.challenge_digest == record.challenge_digest
        if same_challenge and (
            (existing.consumed and not record.consumed)
            or (existing.session_ended and not record.session_ended)
        ):
            raise BypassError("challenge state regression")
        if same_challenge and existing.pending_events and not record.pending_events and not allow_pending_clear:
            record = replace(record, pending_events=existing.pending_events)
    data[record.session_id] = record.to_dict()
    _write_json(path, data)


def save_challenge(paths: Any, record: ChallengeRecord) -> None:
    if not isinstance(record, ChallengeRecord):
        raise TypeError("record must be a ChallengeRecord")
    with _store_lock(paths) as path:
        _save_record_unlocked(path, record)


def load_challenge(paths: Any, session_id: str) -> Optional[ChallengeRecord]:
    _validate_session(session_id)
    with _store_lock(paths) as path:
        data = _read_store(path)
        if session_id not in data:
            return None
        return ChallengeRecord.from_dict(data[session_id])


def issue_challenge(paths: Any, session_id: str, now: Optional[datetime] = None) -> ChallengeRecord:
    record = create_challenge(session_id, _clock(now))
    save_challenge(paths, record)
    return record


def consume_bypass(
    paths: Any,
    session_id: str,
    command: str,
    reason: str = "user requested wizard bypass",
    now: Optional[datetime] = None,
    origin: str = "user_entry",
) -> BypassResult:
    if origin != "user_entry":
        raise BypassError("child session cannot request bypass")
    with _store_lock(paths) as path:
        data = _read_store(path)
        if session_id not in data:
            raise BypassError("no challenge for session")
        record = ChallengeRecord.from_dict(data[session_id])
        if record.consumed and record.pending_events:
            current = _clock(now)
            if record.session_ended or current >= _parse_timestamp(record.expires_at):
                raise BypassError("challenge expired")
            match = _COMMAND.fullmatch(command) if isinstance(command, str) else None
            if not match or not secrets.compare_digest(
                _digest(match.group(1)), record.challenge_digest
            ):
                raise BypassError("challenge command is invalid")
            pending = record.pending_events
            _append_events(paths, pending)
            completed = replace(record, pending_events=())
            _save_record_unlocked(path, completed, allow_pending_clear=True)
            return BypassResult(True, completed, pending)
        result = grant_bypass(record, command, reason, _clock(now))
        staged = replace(result.record, pending_events=result.events)
        _save_record_unlocked(path, staged)
        _append_events(paths, staged.pending_events)
        completed = replace(staged, pending_events=())
        _save_record_unlocked(path, completed, allow_pending_clear=True)
        return BypassResult(True, completed, result.events)


def _append_events(paths: Any, events: Tuple[Dict[str, Any], ...]) -> None:
    directory = paths.vibe
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / _EVENTS
    if path.is_symlink():
        raise BypassError("session event log may not be a symlink")
    lock = directory / ".session-events.lock"
    with open(lock, "a+", encoding="utf-8") as handle:
        flock_exclusive(handle.fileno())
        existing_ids = set()
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as error:
                raise BypassError("session event log is unreadable") from error
            for line in lines:
                try:
                    decoded = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isinstance(decoded, dict):
                    existing_ids.add(_event_identity(decoded))
        with open(path, "a", encoding="utf-8") as stream:
            for event in events:
                event_id = _event_identity(event) if isinstance(event, dict) else None
                if event_id is not None and event_id in existing_ids:
                    continue
                stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                stream.write("\n")
                if event_id is not None:
                    existing_ids.add(event_id)
            stream.flush()
            os.fsync(stream.fileno())
        flock_release(handle.fileno())


def end_session(paths: Any, session_id: str) -> None:
    _validate_session(session_id)
    with _store_lock(paths) as path:
        data = _read_store(path)
        if session_id not in data:
            return
        record = ChallengeRecord.from_dict(data[session_id])
        if not record.session_ended:
            _save_record_unlocked(path, replace(record, session_ended=True, challenge=None))
