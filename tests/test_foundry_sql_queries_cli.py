"""Tests for the 5-operation Foundry SQL Queries CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch,
JSON validation, the two Arrow-byte downloads through BinaryDownloadHandler,
access control write classification and read-only mode, the packaged 1/4
metadata-only policy, attribution suppression, retry and error taxonomy,
timeouts, output formats, privacy, and the console boundary.

All SDK transport is mocked: no live Foundry connection is ever made.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foundry_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from foundry_cli.sql_queries.scripts import foundry_sql_queries_cli as cli


class _Scope:
    """No-op async invocation scope double."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> bool:
        return False


class _Factory:
    """Recording factory double that exposes invocation_scope and create."""

    def __init__(self, root: Any) -> None:
        self.root = root
        self.create_calls = 0
        self.scope_kwargs: dict[str, Any] = {}
        self.create_kwargs: dict[str, Any] = {}

    def invocation_scope(self, cfg: Any, **kwargs: Any) -> _Scope:
        self.scope_kwargs = kwargs
        return _Scope()

    def create(self, cfg: Any, **kwargs: Any) -> Any:
        self.create_calls += 1
        self.create_kwargs = kwargs
        return self.root


class _ImmediateRetry:
    """Retry double that executes the wrapped callable exactly once."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def execute(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await function(*args, **kwargs)


class _Cfg:
    log_level = "ERROR"
    timeout_s = 30
    global_readonly = False
    global_metadata_only = False

    def load(self) -> None:
        return None


def _root() -> tuple[Any, dict[str, AsyncMock]]:
    """Build a nested SqlQueries SDK fake rooted at client.sql_queries."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    sql_query = SimpleNamespace(
        cancel=tracked("cancel"),
        execute=tracked("execute"),
        execute_ontology=tracked("execute_ontology"),
        get_results=tracked("get_results"),
        get_status=tracked("get_status"),
    )
    root = SimpleNamespace(sql_queries=SimpleNamespace(SqlQuery=sql_query))
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_5_operations() -> None:
    assert len(cli.OP_SPECS) == 5
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 5
    assert len(cli.OPERATION_BY_RESOURCE) == 1
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("sql_query", "cancel", ("SqlQuery",), "cancel"),
        ("sql_query", "execute", ("SqlQuery",), "execute"),
        ("sql_query", "execute_ontology", ("SqlQuery",), "execute_ontology"),
        ("sql_query", "get_results", ("SqlQuery",), "get_results"),
        ("sql_query", "get_status", ("SqlQuery",), "get_status"),
    ]


def test_catalog_marks_exactly_two_download_operations() -> None:
    assert cli.DOWNLOAD_OPS == frozenset(
        {
            ("sql_query", "execute_ontology"),
            ("sql_query", "get_results"),
        }
    )


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(["query", "cancel", "ri.q"])
    _parse(["query", "execute", "--query", "select 1"])
    _parse(
        [
            "query",
            "execute",
            "--query",
            "select * from x",
            "--fallback-branch-ids-json",
            '["master"]',
        ]
    )
    _parse(
        [
            "query",
            "execute-ontology",
            "--query",
            "select * from object",
            "--dry-run",
            "--parameters-json",
            "{}",
            "--row-limit",
            "10",
        ]
    )
    _parse(["query", "get-results", "ri.q", "--output", "results.arrow"])
    _parse(["query", "get-status", "ri.q"])
    _parse(["query", "get-status", "ri.q", "--timeout", "60", "--format", "json", "--pretty"])


def test_parser_rejects_unknown_operation() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["query", "list-all"])


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_dispatches_to_sql_query_cancel(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["cancel"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "cancel", "ri.q", "--format", "json"])
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) is None
    calls["cancel"].assert_awaited_once_with("ri.q", request_timeout=30)


@pytest.mark.asyncio
async def test_execute_dispatches_with_query_and_optional_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["execute"].return_value = SimpleNamespace(
        to_dict=lambda: {"status": "RUNNING"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "query",
            "execute",
            "--query",
            "select 1",
            "--fallback-branch-ids-json",
            '["master"]',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "RUNNING"}
    calls["execute"].assert_awaited_once_with(
        query="select 1", fallback_branch_ids=["master"], request_timeout=30
    )


@pytest.mark.asyncio
async def test_get_status_dispatches_and_omits_absent_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get_status"].return_value = SimpleNamespace(
        to_dict=lambda: {"status": "SUCCEEDED"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "get-status", "ri.q", "--format", "json"])
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "SUCCEEDED"}
    calls["get_status"].assert_awaited_once_with("ri.q", request_timeout=30)


@pytest.mark.asyncio
async def test_unknown_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "list-all"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


@pytest.mark.asyncio
async def test_missing_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# JSON validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_fallback_branch_json_rejected_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "query", "execute", "--query", "select 1", "--fallback-branch-ids-json", "not-json"],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_invalid_parameters_json_rejected_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "query", "execute-ontology", "--query", "select 1", "--parameters-json", "[]"],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Arrow downloads
# --------------------------------------------------------------------------


class _StreamResp:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def aiter_bytes(self) -> Any:
        for chunk in self.chunks:
            yield chunk


class _StreamCtx:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    def __call__(self, *args: Any, **kwargs: Any) -> "_StreamCtx":
        return self

    async def __aenter__(self) -> _StreamResp:
        return _StreamResp(self.chunks)

    async def __aexit__(self, *args: Any) -> bool:
        self.closed = True
        return False


class _StreamingSqlQuery:
    def __init__(self, chunks: list[bytes]) -> None:
        self.ctx = _StreamCtx(chunks)

    def execute_ontology(self, *args: Any, **kwargs: Any) -> _StreamCtx:
        return self.ctx

    def get_results(self, *args: Any, **kwargs: Any) -> _StreamCtx:
        return self.ctx


@pytest.mark.asyncio
async def test_execute_ontology_writes_atomically_and_reports_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    streaming = _StreamingSqlQuery([b"arrow-bytes"])
    sql_query = SimpleNamespace(with_streaming_response=streaming)
    root = SimpleNamespace(sql_queries=SimpleNamespace(SqlQuery=sql_query))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)

    class _CfgDownload(_Cfg):
        download_path = str(tmp_path)
        max_download_bytes = 100

    monkeypatch.setattr(cli, "ConfigLoader", _CfgDownload)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "query",
            "execute-ontology",
            "--query",
            "select 1",
            "--row-limit",
            "10",
        ],
    )
    assert await cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file_size"] == 11
    assert out["truncated"] is False
    saved = Path(out["file_path"])
    assert saved.read_bytes() == b"arrow-bytes"
    assert streaming.ctx.closed


@pytest.mark.asyncio
async def test_get_results_download_requires_output_and_closes_response(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    streaming = _StreamingSqlQuery([b"abc"])
    sql_query = SimpleNamespace(with_streaming_response=streaming)
    root = SimpleNamespace(sql_queries=SimpleNamespace(SqlQuery=sql_query))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)

    class _CfgDownload(_Cfg):
        download_path = str(tmp_path)
        max_download_bytes = 100

    monkeypatch.setattr(cli, "ConfigLoader", _CfgDownload)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "query", "get-results", "ri.q", "--output", "r.arrow"],
    )
    assert await cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file_size"] == 3
    saved = Path(out["file_path"])
    assert saved.read_bytes() == b"abc"
    assert streaming.ctx.closed


@pytest.mark.asyncio
async def test_download_rejects_unsafe_filename(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    streaming = _StreamingSqlQuery([b"abc"])
    sql_query = SimpleNamespace(with_streaming_response=streaming)
    root = SimpleNamespace(sql_queries=SimpleNamespace(SqlQuery=sql_query))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)

    class _CfgDownload(_Cfg):
        download_path = str(tmp_path)
        max_download_bytes = 100

    monkeypatch.setattr(cli, "ConfigLoader", _CfgDownload)
    for unsafe in ("../escape", "..\\escape", "/absolute", "."):
        monkeypatch.setattr(
            sys,
            "argv",
            ["cmd", "query", "get-results", "ri.q", "--output", unsafe],
        )
        assert await cli.main() == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_blocks_three_write_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    writes = [
        ["query", "cancel", "ri.q"],
        ["query", "execute", "--query", "select 1"],
        ["query", "execute-ontology", "--query", "select 1"],
    ]
    for argv in writes:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 8
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_semantic_reads_permitted_under_readonly(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")

    streaming = _StreamingSqlQuery([b"abc"])
    sql_query = SimpleNamespace(
        get_status=AsyncMock(
            return_value=SimpleNamespace(to_dict=lambda: {"status": "RUNNING"})
        ),
        with_streaming_response=streaming,
    )
    root = SimpleNamespace(sql_queries=SimpleNamespace(SqlQuery=sql_query))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)

    # get_status is a semantic read and passes under READONLY.
    monkeypatch.setattr(
        sys, "argv", ["cmd", "query", "get-status", "ri.q", "--format", "json"]
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"status": "RUNNING"}

    # get_results is also a semantic read and streams its download under
    # READONLY without an ACL block.
    class _CfgDownload(_Cfg):
        download_path = str(tmp_path)
        max_download_bytes = 100

    monkeypatch.setattr(cli, "ConfigLoader", _CfgDownload)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "query", "get-results", "ri.q", "--output", "r.arrow"]
    )
    assert await cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file_size"] == 3


@pytest.mark.asyncio
async def test_acl_denial_reports_rule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "execute", "--query", "select 1"])
    assert await cli.main() == 8
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 8
    assert "metadata-only mode active" in envelope["message"]


def test_metadata_only_permits_exactly_1_blocks_4() -> None:
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {("sql_query", "get_status")}
    allowed_path = Path(cli._METADATA_ALLOWLIST_PATH)
    assert allowed_path.exists()
    text = allowed_path.read_text(encoding="utf-8")
    parsed: set[tuple[str, str]] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if "PERMITTED" not in stripped:
            continue
        path = stripped.split("|")[1].strip().strip("`")
        resource, operation = path.split(".")[1:]
        parsed.add((resource, operation))
    assert parsed == permitted
    blocked = catalog - permitted
    assert blocked == {
        ("sql_query", "cancel"),
        ("sql_query", "execute"),
        ("sql_query", "execute_ontology"),
        ("sql_query", "get_results"),
    }


def test_metadata_only_runtime_blocks_blocked_ops_and_permits_get_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    guard = AccessControlGuard(_Cfg(), "SQL_QUERIES")
    for resource, operation in [
        ("sql_query", "cancel"),
        ("sql_query", "execute"),
        ("sql_query", "execute_ontology"),
        ("sql_query", "get_results"),
    ]:
        with pytest.raises(AccessControlError):
            guard.check(resource, operation)
    guard.check("sql_query", "get_status")


# --------------------------------------------------------------------------
# Attribution suppression
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_uses_include_attribution_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get_status"].return_value = SimpleNamespace(
        to_dict=lambda: {"status": "RUNNING"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "get-status", "ri.q", "--format", "json"])
    assert await cli.main() == 0
    assert factory.scope_kwargs == {"include_attribution": False}
    assert factory.create_kwargs == {"include_attribution": False}


# --------------------------------------------------------------------------
# Timeouts, errors, output, console
# --------------------------------------------------------------------------


def test_timeout_accepts_adr_002_bounds() -> None:
    assert cli._validate_timeout(1) == 1
    assert cli._validate_timeout(3600) == 3600
    for invalid in (0, 3601, -1, True):
        with pytest.raises(cli.CLIInputError):
            cli._validate_timeout(invalid)


@pytest.mark.asyncio
async def test_invalid_timeout_stops_before_acl_or_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "get-status", "ri.q", "--timeout", "0"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_sdk_error_maps_to_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get_status"].side_effect = TimeoutError("timed out")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "get-status", "ri.q"])
    assert await cli.main() == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 5


@pytest.mark.asyncio
async def test_output_toon_and_json_formats(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get_status"].return_value = SimpleNamespace(
        to_dict=lambda: {"status": "SUCCEEDED"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "get-status", "ri.q", "--format", "toon"])
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert "status" in out


@pytest.mark.asyncio
async def test_console_main_uses_one_asyncio_run_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_run(coro: Any) -> int:
        calls.append("run")
        return 7

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    monkeypatch.setattr(cli, "main", lambda: 7)
    assert cli.console_main() == 7
    assert calls == ["run"]


@pytest.mark.asyncio
async def test_sensitive_values_not_echoed_in_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get_status"].side_effect = Exception("secret-token leaked")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "query", "get-status", "ri.q"])
    assert await cli.main() == 6
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message"] == "SqlQueries operation failed"
    assert "secret-token" not in envelope["message"]
