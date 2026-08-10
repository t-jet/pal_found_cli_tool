"""Tests for the 3-operation Foundry Checkpoints CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch,
JSON validation, PaginationHelper cursor paging on ``record search``, access
control read classification and metadata-only policy (3/3 permitted),
attribution suppression, retry and error taxonomy, timeouts, output
formats, privacy, and the console boundary.

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
from foundry_cli.checkpoints.scripts import foundry_checkpoints_cli as cli


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
    """Build a nested Checkpoints SDK fake rooted at client.checkpoints."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    record = SimpleNamespace(
        get=tracked("get"),
        get_batch=tracked("get_batch"),
        search=tracked("search"),
    )
    root = SimpleNamespace(checkpoints=SimpleNamespace(Record=record))
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_3_operations() -> None:
    assert len(cli.OP_SPECS) == 3
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 3
    assert len(cli.OPERATION_BY_RESOURCE) == 1
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("record", "get", ("Record",), "get"),
        ("record", "get_batch", ("Record",), "get_batch"),
        ("record", "search", ("Record",), "search"),
    ]


def test_catalog_marks_exactly_one_paginated_operation() -> None:
    assert cli.PAGINATED_OPS == frozenset({("record", "search")})


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(["record", "get", "ri.checks.main.record.x"])
    _parse(
        [
            "record",
            "get-batch",
            "--records-json",
            '[{"recordRid": "ri.checks.main.record.x"}]',
        ]
    )
    _parse(
        [
            "record",
            "search",
            "--where-json",
            '{"filter": {"type": "eq", "field": "recordRid", "value": "ri.checks.main.record.x"}}',
            "--page-size",
            "50",
            "--page-token",
            "tok",
            "--all",
            "--max-pages",
            "3",
            "--sort-direction",
            "ASC",
        ]
    )
    _parse(
        [
            "record",
            "get",
            "ri.checks.main.record.x",
            "--timeout",
            "60",
            "--format",
            "json",
            "--pretty",
        ]
    )


def test_parser_rejects_unknown_operation() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["record", "list-all"])


def test_pagination_flags_only_on_record_search() -> None:
    get_args = _parse(["record", "get", "ri.checks.main.record.x"])
    assert not hasattr(get_args, "page_size")
    assert not hasattr(get_args, "page_token")
    assert not hasattr(get_args, "all_pages")
    assert not hasattr(get_args, "max_pages")
    search_args = _parse(
        [
            "record",
            "search",
            "--where-json",
            '{"filter": {"type": "eq", "field": "recordRid", "value": "x"}}',
        ]
    )
    assert search_args.page_size is None
    assert search_args.page_token is None
    assert search_args.all_pages is False
    assert search_args.max_pages is None


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dispatches_to_record_get(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.r"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "record", "get", "ri.checks.main.record.x", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.r"}
    calls["get"].assert_awaited_once_with(
        "ri.checks.main.record.x", request_timeout=30
    )


@pytest.mark.asyncio
async def test_get_batch_dispatches_body_positionally(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get_batch"].return_value = SimpleNamespace(
        to_dict=lambda: {"data": {"ri.r": {"rid": "ri.r"}}}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "record",
            "get-batch",
            "--records-json",
            '[{"recordRid": "ri.checks.main.record.x"}]',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"data": {"ri.r": {"rid": "ri.r"}}}
    calls["get_batch"].assert_awaited_once_with(
        [{"recordRid": "ri.checks.main.record.x"}], request_timeout=30
    )


@pytest.mark.asyncio
async def test_search_dispatches_where_and_optionals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, raw = _paged_root(
        [([{"rid": "ri.r"}], None)]
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    where = '{"filter": {"type": "eq", "field": "recordRid", "value": "ri.r"}}'
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "record",
            "search",
            "--where-json",
            where,
            "--sort-direction",
            "ASC",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out == [{"rid": "ri.r"}]
    assert len(raw.calls) == 1
    assert raw.calls[0]["where"] == {
        "filter": {"type": "eq", "field": "recordRid", "value": "ri.r"}
    }
    assert raw.calls[0]["sort_direction"] == "ASC"


@pytest.mark.asyncio
async def test_unknown_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "record", "list-all"])
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
    monkeypatch.setattr(sys, "argv", ["cmd", "record"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# JSON validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_where_json_rejected_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "record", "search", "--where-json", "not-json"],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_where_json_must_be_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "search", "--where-json", "[1, 2]"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_invalid_records_json_rejected_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "get-batch", "--records-json", "{}"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


class _Page:
    def __init__(self, items: list[Any], next_page_token: str | None) -> None:
        self.data = items
        self.next_page_token = next_page_token

    def decode(self) -> "_Page":
        return self


class _RawRecord:
    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> _Page:
        self.calls.append(kwargs)
        if not self.pages:
            return _Page([], None)
        items, nxt = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]
        return _Page(items, nxt)


def _paged_root(pages: list[tuple[list[Any], str | None]]) -> Any:
    raw = _RawRecord(pages)
    record = SimpleNamespace(with_raw_response=SimpleNamespace(search=raw))
    return SimpleNamespace(checkpoints=SimpleNamespace(Record=record)), raw


@pytest.mark.asyncio
async def test_record_search_uses_raw_response_and_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, raw = _paged_root([(["a", "b"], "tok1"), (["c"], None)])
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "record",
            "search",
            "--where-json",
            '{"filter": {"type": "eq", "field": "recordRid", "value": "ri.r"}}',
            "--max-pages",
            "5",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == ["a", "b", "c"]
    assert len(raw.calls) == 2
    assert raw.calls[0]["page_token"] is None
    assert raw.calls[1]["page_token"] == "tok1"
    assert raw.calls[0]["page_size"] == 100


@pytest.mark.asyncio
async def test_record_search_defaults_to_single_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, raw = _paged_root([(["x"], "tok2"), (["y"], None)])
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "record",
            "search",
            "--where-json",
            '{"filter": {"type": "eq", "field": "recordRid", "value": "ri.r"}}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == ["x"]
    assert len(raw.calls) == 1


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_permits_all_three_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.r"})
    calls["get_batch"].return_value = SimpleNamespace(to_dict=lambda: {"data": {}})
    calls["search"].return_value = SimpleNamespace(to_dict=lambda: {"data": []})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "get", "ri.checks.main.record.x", "--format", "json"]
    )
    assert await cli.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "record",
            "get-batch",
            "--records-json",
            '[{"recordRid": "ri.checks.main.record.x"}]',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "record",
            "search",
            "--where-json",
            '{"filter": {"type": "eq", "field": "recordRid", "value": "ri.r"}}',
            "--format",
            "json",
        ],
    )
    root2, _ = _paged_root([([{"rid": "ri.r"}], None)])
    factory2 = _Factory(root2)
    _patch_main(monkeypatch, factory2)
    assert await cli.main() == 0


def test_metadata_only_permits_exactly_3_blocks_0() -> None:
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("record", "get"),
        ("record", "get_batch"),
        ("record", "search"),
    }
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
    assert catalog - permitted == set()


def test_metadata_only_runtime_permits_all_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    guard = AccessControlGuard(_Cfg(), "CHECKPOINTS")
    for resource, operation in [
        ("record", "get"),
        ("record", "get_batch"),
        ("record", "search"),
    ]:
        guard.check(resource, operation)


# --------------------------------------------------------------------------
# Attribution suppression
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_uses_include_attribution_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.r"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "get", "ri.checks.main.record.x", "--format", "json"]
    )
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
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "get", "ri.checks.main.record.x", "--timeout", "0"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_sdk_error_maps_to_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].side_effect = TimeoutError("timed out")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "get", "ri.checks.main.record.x"]
    )
    assert await cli.main() == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 5


@pytest.mark.asyncio
async def test_output_toon_and_json_formats(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.r"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "get", "ri.checks.main.record.x", "--format", "toon"]
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert "rid" in out


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
    calls["get"].side_effect = Exception("secret-token leaked")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "record", "get", "ri.checks.main.record.x"]
    )
    assert await cli.main() == 6
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message"] == "Checkpoints operation failed"
    assert "secret-token" not in envelope["message"]
