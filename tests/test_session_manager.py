"""Focused tests for secure, atomic session state."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

SRC = Path(__file__).parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pal_found_cli.common.session_manager as session_module
from pal_found_cli.common.session_manager import (
    InvalidSessionAliasError,
    SessionAliasConflictError,
    SessionCorruptionError,
    SessionManager,
    SessionPersistenceError,
    SessionState,
)


def _state(**overrides: object) -> SessionState:
    now = datetime.now(UTC).isoformat()
    values = {
        "session_id": "session-rid",
        "agent_rid": "agent-rid",
        "session_token": None,
        "created_at": now,
        "last_used_at": now,
        "status": "active",
        "tool_history": [],
    }
    values.update(overrides)
    return SessionState(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("  My   Session  ", "my-session"), ("ＦＯＯ", "foo"), ("A.B_c-9", "a.b_c-9")],
)
def test_alias_normalization_is_stable(raw: str, canonical: str) -> None:
    assert SessionManager.normalize_alias(raw) == canonical


@pytest.mark.parametrize(
    "alias",
    [".", "..", "../escape", r"..\escape", "CON", "name/child", "é", "a" * 65],
)
def test_aliases_that_can_escape_or_confuse_filesystems_are_rejected(
    alias: str,
) -> None:
    with pytest.raises(InvalidSessionAliasError):
        SessionManager.normalize_alias(alias)


@pytest.mark.asyncio
async def test_create_persists_remote_rid_and_null_token(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    create_remote = AsyncMock(return_value=SimpleNamespace(rid="ri.session.123"))

    state = await manager.create(" Demo Session ", "ri.agent.1", create_remote)

    assert state.session_id == "ri.session.123"
    assert state.session_token is None
    assert manager.load("demo session") == state
    persisted = json.loads((tmp_path / "demo-session.json").read_text(encoding="utf-8"))
    assert persisted["session_token"] is None
    create_remote.assert_awaited_once_with()


@pytest.mark.parametrize("token_marker", ["missing", None, "legacy-secret"])
def test_load_accepts_compatible_token_forms(
    tmp_path: Path, token_marker: object
) -> None:
    manager = SessionManager(tmp_path)
    record = _state(session_token=None).to_dict()
    if token_marker == "missing":
        record.pop("session_token")
        expected = None
    else:
        record["session_token"] = token_marker
        expected = token_marker
    (tmp_path / "alias.json").write_text(json.dumps(record), encoding="utf-8")

    loaded = manager.load("alias")

    assert loaded.session_token == expected
    assert loaded.session_id == "session-rid"


@pytest.mark.asyncio
async def test_concurrent_create_has_one_remote_call_and_one_conflict(
    tmp_path: Path,
) -> None:
    manager = SessionManager(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def create_remote() -> SimpleNamespace:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return SimpleNamespace(rid="only-session")

    first = asyncio.create_task(manager.create("same alias", "agent", create_remote))
    await entered.wait()
    second = asyncio.create_task(manager.create("SAME   ALIAS", "agent", create_remote))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert calls == 1
    assert sum(isinstance(item, SessionState) for item in results) == 1
    assert sum(isinstance(item, SessionAliasConflictError) for item in results) == 1
    assert manager.load("same-alias").session_id == "only-session"


@pytest.mark.asyncio
async def test_persistence_failure_compensates_and_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(tmp_path)
    delete_remote = AsyncMock()

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(session_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="disk full") as caught:
        await manager.create(
            "alias",
            "agent",
            AsyncMock(return_value=SimpleNamespace(rid="remote-rid")),
            delete_remote,
        )

    assert not hasattr(caught.value, "diagnostic_metadata")
    delete_remote.assert_awaited_once_with("remote-rid")
    assert list(tmp_path.glob("*.json")) == []
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_failed_compensation_exposes_rid_but_preserves_persistence_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(tmp_path)
    monkeypatch.setattr(
        session_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("disk full")),
    )
    delete_remote = AsyncMock(side_effect=RuntimeError("remote delete failed"))

    with pytest.raises(OSError, match="disk full") as caught:
        await manager.create(
            "alias",
            "agent",
            AsyncMock(return_value=SimpleNamespace(rid="remote-rid")),
            delete_remote,
        )

    assert caught.value.diagnostic_metadata == {"session_id": "remote-rid"}
    assert any("session_id=remote-rid" in note for note in caught.value.__notes__)


def test_atomic_update_failure_keeps_previous_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(tmp_path)
    original = _state()
    manager.update("alias", original)
    original_bytes = (tmp_path / "alias.json").read_bytes()
    monkeypatch.setattr(
        session_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        manager.update("alias", replace(original, status="completed"))

    assert (tmp_path / "alias.json").read_bytes() == original_bytes
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_record_is_deleted_without_logging_secret(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager(tmp_path)
    secret = "do-not-log-this-token"
    path = tmp_path / "alias.json"
    path.write_text(
        json.dumps({"session_token": secret, "status": "not-valid"}),
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING), pytest.raises(SessionCorruptionError):
        manager.load("alias")

    assert path.exists() is False
    assert secret not in caplog.text
    assert all(secret not in repr(record.__dict__) for record in caplog.records)


def test_cleanup_expired_uses_utc_boundary_and_purge_is_idempotent(
    tmp_path: Path,
) -> None:
    manager = SessionManager(tmp_path, expiry_days=7)
    now = datetime(2026, 7, 27, tzinfo=UTC)
    expired = _state(
        session_id="expired",
        last_used_at=(now - timedelta(days=7)).isoformat(),
    )
    active = _state(
        session_id="active",
        last_used_at=(now - timedelta(days=7) + timedelta(microseconds=1)).isoformat(),
    )
    manager.update("expired", expired)
    manager.update("active", active)

    assert manager.cleanup_expired(now) == 1
    assert (tmp_path / "expired.json").exists() is False
    assert manager.load("active").session_id == "active"
    assert manager.purge() == 1
    assert manager.purge() == 0


def test_cleanup_rejects_naive_clock(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    with pytest.raises(ValueError, match="timezone-aware"):
        manager.cleanup_expired(datetime(2026, 7, 27))
