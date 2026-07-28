"""Atomic local session persistence with cross-process alias locking."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from foundry_cli.common.config_loader import ConfigLoader

logger = logging.getLogger(__name__)

_ALIAS_PATTERN = re.compile(r"(?:[a-z0-9]|[a-z0-9][a-z0-9._-]{0,62}[a-z0-9])\Z")
_WINDOWS_RESERVED = {
    "aux",
    "clock$",
    "con",
    "nul",
    "prn",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_VALID_STATUSES = {"active", "completed", "expired"}


class RemoteSession(Protocol):
    """Minimum SDK session shape required by ``SessionManager``."""

    rid: str


class SessionError(Exception):
    """Base error for local session management."""

    exit_code = 1


class InvalidSessionAliasError(SessionError, ValueError):
    """Raised when an alias cannot be normalized safely."""


class SessionAliasConflictError(SessionError):
    """Raised when an active session already owns an alias."""


class SessionNotFoundError(SessionError, FileNotFoundError):
    """Raised when no local state exists for an alias."""

    exit_code = 4


class SessionCorruptionError(SessionError):
    """Raised after corrupt local state has been removed safely."""


class SessionPersistenceError(SessionError):
    """Raised when remote creation succeeds but local persistence fails."""

    exit_code = 6

    def __init__(self, message: str, *, session_id: str | None = None) -> None:
        super().__init__(message)
        self.diagnostic_metadata = (
            {"session_id": session_id} if session_id is not None else {}
        )


@dataclass
class SessionState:
    """Persisted AIP Agents session state."""

    session_id: str
    agent_rid: str
    session_token: str | None
    created_at: str
    last_used_at: str
    status: Literal["active", "completed", "expired"]
    tool_history: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable copy of this state."""
        return asdict(self)


class _AliasLock:
    """Small platform lock wrapper backed by a persistent lock file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: Any = None

    def acquire(self, *, blocking: bool) -> bool:
        """Acquire the lock, returning ``False`` for a nonblocking conflict."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        file_obj = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                file_obj.seek(0, os.SEEK_END)
                if file_obj.tell() == 0:
                    file_obj.write(b"\0")
                    file_obj.flush()
                file_obj.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(file_obj.fileno(), mode, 1)
                except OSError:
                    file_obj.close()
                    return False
            else:
                fcntl: Any = importlib.import_module("fcntl")

                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                try:
                    fcntl.flock(file_obj.fileno(), flags)
                except BlockingIOError:
                    file_obj.close()
                    return False
        except BaseException:
            file_obj.close()
            raise
        self._file = file_obj
        return True

    def release(self) -> None:
        """Release this process's lock and close its descriptor."""
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> _AliasLock:
        if not self.acquire(blocking=True):
            raise SessionError(
                f"Could not acquire session alias lock: {self.path.name}"
            )
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class SessionManager:
    """Persist session aliases as validated, atomic JSON records."""

    def __init__(
        self,
        session_root: str | Path | None = None,
        *,
        config: ConfigLoader | None = None,
        expiry_days: int = 7,
    ) -> None:
        cfg = config or ConfigLoader()
        self.session_root = Path(
            session_root if session_root is not None else cfg.session_path
        ).expanduser()
        if expiry_days <= 0:
            raise ValueError("expiry_days must be positive")
        self.expiry = timedelta(days=expiry_days)
        self._ensure_root()

    async def create(
        self,
        alias: str,
        agent_rid: str,
        create_remote: Callable[[], Awaitable[RemoteSession]],
        delete_remote: Callable[[str], Awaitable[None]] | None = None,
    ) -> SessionState:
        """Create one remote session and bind it atomically to ``alias``."""
        canonical = self.normalize_alias(alias)
        lock = self._lock(canonical)
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.05)
        try:
            try:
                current = self._load_locked(canonical)
            except SessionNotFoundError:
                current = None
            except SessionCorruptionError:
                current = None
            if current is not None and current.status == "active":
                raise SessionAliasConflictError(
                    f"Active session alias already exists: {canonical}"
                )

            remote = await create_remote()
            session_id = getattr(remote, "rid", None)
            if not isinstance(session_id, str) or not session_id.strip():
                raise SessionPersistenceError(
                    "Remote session did not return a valid rid"
                )
            now = datetime.now(UTC).isoformat()
            state = SessionState(
                session_id=session_id,
                agent_rid=agent_rid,
                session_token=None,
                created_at=now,
                last_used_at=now,
                status="active",
                tool_history=[],
            )
            try:
                self._write_locked(canonical, state)
            except BaseException as persistence_error:
                compensation_error: BaseException | None = None
                if delete_remote is not None:
                    try:
                        await delete_remote(session_id)
                    except BaseException as exc:
                        compensation_error = exc
                if compensation_error is not None:
                    persistence_error.add_note(
                        f"Remote session compensation failed for session_id={session_id}"
                    )
                    setattr(
                        persistence_error,
                        "diagnostic_metadata",
                        {"session_id": session_id},
                    )
                    logger.error(
                        "Remote session compensation failed",
                        extra={"session_id": session_id},
                    )
                raise

            self._warn_if_many_active(agent_rid)
            return state
        finally:
            lock.release()

    def load(self, alias: str) -> SessionState:
        """Load and validate an alias record."""
        canonical = self.normalize_alias(alias)
        with self._lock(canonical):
            return self._load_locked(canonical)

    def update(self, alias: str, state: SessionState) -> None:
        """Atomically replace a validated alias record."""
        canonical = self.normalize_alias(alias)
        with self._lock(canonical):
            self._validate_state(state)
            self._write_locked(canonical, state)

    def purge(self) -> int:
        """Delete all unlocked session records and return deletion count."""
        deleted = 0
        for path in self.session_root.glob("*.json"):
            canonical = path.stem
            lock = self._lock(canonical)
            if not lock.acquire(blocking=False):
                logger.warning(
                    "Skipped locked session during purge",
                    extra={"session_alias": canonical},
                )
                continue
            try:
                if path.exists():
                    path.unlink()
                    deleted += 1
            finally:
                lock.release()
        return deleted

    def cleanup_expired(self, now: datetime | None = None) -> int:
        """Delete records inactive for the configured UTC expiry period."""
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_time = current_time.astimezone(UTC)
        deleted = 0
        for path in self.session_root.glob("*.json"):
            canonical = path.stem
            lock = self._lock(canonical)
            if not lock.acquire(blocking=False):
                logger.warning(
                    "Skipped locked session during expiry cleanup",
                    extra={"session_alias": canonical},
                )
                continue
            try:
                try:
                    state = self._load_locked(canonical)
                except (SessionNotFoundError, SessionCorruptionError):
                    continue
                last_used = self._parse_timestamp(state.last_used_at)
                if state.status == "expired" or current_time - last_used >= self.expiry:
                    path.unlink(missing_ok=True)
                    deleted += 1
            finally:
                lock.release()
        return deleted

    @classmethod
    def normalize_alias(cls, alias: str) -> str:
        """Return a canonical, contained session alias."""
        if not isinstance(alias, str):
            raise InvalidSessionAliasError("Session alias must be a string")
        normalized = unicodedata.normalize("NFKC", alias).strip().casefold()
        normalized = re.sub(r"\s+", "-", normalized)
        if (
            not normalized.isascii()
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or any(
                ord(character) < 32 or ord(character) == 127 for character in normalized
            )
            or _ALIAS_PATTERN.fullmatch(normalized) is None
            or normalized.split(".", maxsplit=1)[0] in _WINDOWS_RESERVED
        ):
            raise InvalidSessionAliasError("Session alias is invalid")
        return normalized

    def _ensure_root(self) -> None:
        self.session_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.session_root, 0o700)

    def _path(self, canonical: str) -> Path:
        path = (self.session_root / f"{canonical}.json").resolve()
        if path.parent != self.session_root.resolve():
            raise InvalidSessionAliasError("Session alias escapes configured path")
        return path

    def _lock(self, canonical: str) -> _AliasLock:
        return _AliasLock(self.session_root / ".locks" / f"{canonical}.lock")

    def _load_locked(self, canonical: str) -> SessionState:
        path = self._path(canonical)
        if not path.exists():
            raise SessionNotFoundError(f"Session alias not found: {canonical}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("record is not an object")
            token = raw.get("session_token")
            if token is not None and not isinstance(token, str):
                raise ValueError("session_token has invalid type")
            state = SessionState(
                session_id=self._required_string(raw, "session_id"),
                agent_rid=self._required_string(raw, "agent_rid"),
                session_token=token,
                created_at=self._required_string(raw, "created_at"),
                last_used_at=self._required_string(raw, "last_used_at"),
                status=cast(Any, raw.get("status")),
                tool_history=self._tool_history(raw.get("tool_history")),
            )
            self._validate_state(state)
            return state
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            KeyError,
        ) as exc:
            logger.warning(
                "Removed corrupt session state",
                extra={"session_alias": canonical},
            )
            path.unlink(missing_ok=True)
            raise SessionCorruptionError(
                f"Session state is corrupt for alias: {canonical}"
            ) from exc

    def _write_locked(self, canonical: str, state: SessionState) -> None:
        self._validate_state(state)
        path = self._path(canonical)
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{canonical}-", suffix=".tmp", dir=self.session_root
        )
        temp_path = Path(raw_temp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as target:
                json.dump(
                    state.to_dict(), target, ensure_ascii=True, separators=(",", ":")
                )
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            if os.name != "nt":
                os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

    @classmethod
    def _validate_state(cls, state: SessionState) -> None:
        if not state.session_id or not state.agent_rid:
            raise ValueError("session_id and agent_rid are required")
        if state.session_token is not None and not isinstance(state.session_token, str):
            raise ValueError("session_token must be null or a string")
        if state.status not in _VALID_STATUSES:
            raise ValueError("session status is invalid")
        cls._parse_timestamp(state.created_at)
        cls._parse_timestamp(state.last_used_at)
        if not isinstance(state.tool_history, list) or not all(
            isinstance(item, dict) for item in state.tool_history
        ):
            raise ValueError("tool_history must be a list of objects")

    @staticmethod
    def _parse_timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("session timestamps must include a UTC offset")
        return parsed.astimezone(UTC)

    @staticmethod
    def _required_string(raw: dict[str, Any], key: str) -> str:
        value = raw[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _tool_history(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise ValueError("tool_history must be a list of objects")
        return cast(list[dict[str, Any]], value)

    def _warn_if_many_active(self, agent_rid: str) -> None:
        active = 0
        for path in self.session_root.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if raw.get("agent_rid") == agent_rid and raw.get("status") == "active":
                active += 1
        if active > 5:
            logger.warning(
                "Agent has more than five active sessions",
                extra={"active_session_count": active},
            )
