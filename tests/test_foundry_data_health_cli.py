"""Tests for the 6-operation Foundry Data Health CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch
(Check + Check.CheckReport), JSON validation of ``--config-json``, access
control write classification (create/delete/replace) and the metadata-only
policy (3 permitted / 3 blocked), attribution suppression, retry and error
taxonomy, timeouts, output formats, privacy, and the console boundary.

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
from foundry_cli.data_health.scripts import foundry_data_health_cli as cli


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
    """Build a nested Data Health SDK fake rooted at client.data_health."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    check_report = SimpleNamespace(
        get=tracked("check_report_get"),
        get_latest=tracked("check_report_get_latest"),
    )
    check = SimpleNamespace(
        create=tracked("create"),
        delete=tracked("delete"),
        get=tracked("get"),
        replace=tracked("replace"),
        CheckReport=check_report,
    )
    root = SimpleNamespace(data_health=SimpleNamespace(Check=check))
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_6_operations() -> None:
    assert len(cli.OP_SPECS) == 6
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 6
    assert len(cli.OPERATION_BY_RESOURCE) == 2
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("check", "create", ("Check",), "create"),
        ("check", "delete", ("Check",), "delete"),
        ("check", "get", ("Check",), "get"),
        ("check", "replace", ("Check",), "replace"),
        ("check_report", "get", ("Check", "CheckReport"), "get"),
        ("check_report", "get_latest", ("Check", "CheckReport"), "get_latest"),
    ]


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(
        [
            "check",
            "create",
            "--config-json",
            '{"type": "jobStatus", "subject": {"datasetRid": "ri.foundry.main.dataset.x", "branchId": "master"}, "statusCheckConfig": {"severity": "CRITICAL"}}',
            "--intent",
            "weekly health",
        ]
    )
    _parse(["check", "delete", "ri.data-health.main.check.x"])
    _parse(["check", "get", "ri.data-health.main.check.x"])
    _parse(
        [
            "check",
            "replace",
            "ri.data-health.main.check.x",
            "--config-json",
            '{"type": "jobStatus", "subject": {"datasetRid": "ri.foundry.main.dataset.x", "branchId": "master"}, "statusCheckConfig": {"severity": "CRITICAL"}}',
        ]
    )
    _parse(
        [
            "check-report",
            "get",
            "ri.data-health.main.check.x",
            "ri.data-health.main.check-report.x",
        ]
    )
    _parse(
        [
            "check-report",
            "get-latest",
            "ri.data-health.main.check.x",
            "--limit",
            "5",
            "--timeout",
            "60",
            "--format",
            "json",
            "--pretty",
        ]
    )


def test_parser_rejects_unknown_operation() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["check", "list-all"])


def test_no_pagination_flags_anywhere() -> None:
    args = _parse(
        [
            "check-report",
            "get-latest",
            "ri.data-health.main.check.x",
        ]
    )
    assert not hasattr(args, "page_size")
    assert not hasattr(args, "page_token")
    assert not hasattr(args, "all_pages")
    assert not hasattr(args, "max_pages")


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_create_dispatches_config_and_intent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["create"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.c"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check",
            "create",
            "--config-json",
            '{"type": "jobStatus", "subject": {"datasetRid": "ri.foundry.main.dataset.x", "branchId": "master"}, "statusCheckConfig": {"severity": "CRITICAL"}}',
            "--intent",
            "weekly health",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.c"}
    calls["create"].assert_awaited_once_with(
        config={
            "type": "jobStatus",
            "subject": {
                "datasetRid": "ri.foundry.main.dataset.x",
                "branchId": "master",
            },
            "statusCheckConfig": {"severity": "CRITICAL"},
        },
        intent="weekly health",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_check_create_omits_absent_intent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["create"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.c"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check",
            "create",
            "--config-json",
            '{"type": "jobStatus", "subject": {"datasetRid": "ri.foundry.main.dataset.x", "branchId": "master"}, "statusCheckConfig": {"severity": "CRITICAL"}}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["create"].assert_awaited_once_with(
        config={
            "type": "jobStatus",
            "subject": {
                "datasetRid": "ri.foundry.main.dataset.x",
                "branchId": "master",
            },
            "statusCheckConfig": {"severity": "CRITICAL"},
        },
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_check_delete_dispatches_to_check_delete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["delete"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "check", "delete", "ri.data-health.main.check.x", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) is None
    calls["delete"].assert_awaited_once_with(
        "ri.data-health.main.check.x", request_timeout=30
    )


@pytest.mark.asyncio
async def test_check_get_dispatches_to_check_get(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.c"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "check", "get", "ri.data-health.main.check.x", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.c"}
    calls["get"].assert_awaited_once_with(
        "ri.data-health.main.check.x", request_timeout=30
    )


@pytest.mark.asyncio
async def test_check_replace_dispatches_with_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["replace"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.c"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check",
            "replace",
            "ri.data-health.main.check.x",
            "--config-json",
            '{"type": "jobStatus", "subject": {"datasetRid": "ri.foundry.main.dataset.x", "branchId": "master"}, "statusCheckConfig": {"severity": "WARNING"}}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.c"}
    calls["replace"].assert_awaited_once_with(
        "ri.data-health.main.check.x",
        config={
            "type": "jobStatus",
            "subject": {
                "datasetRid": "ri.foundry.main.dataset.x",
                "branchId": "master",
            },
            "statusCheckConfig": {"severity": "WARNING"},
        },
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_check_report_get_dispatches_through_nested_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["check_report_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.r", "result": {"status": "PASSED"}}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check-report",
            "get",
            "ri.data-health.main.check.x",
            "ri.data-health.main.check-report.y",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "rid": "ri.r",
        "result": {"status": "PASSED"},
    }
    calls["check_report_get"].assert_awaited_once_with(
        "ri.data-health.main.check.x",
        "ri.data-health.main.check-report.y",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_check_report_get_latest_forwards_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["check_report_get_latest"].return_value = SimpleNamespace(
        to_dict=lambda: {"data": []}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check-report",
            "get-latest",
            "ri.data-health.main.check.x",
            "--limit",
            "5",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"data": []}
    calls["check_report_get_latest"].assert_awaited_once_with(
        "ri.data-health.main.check.x", limit=5, request_timeout=30
    )


@pytest.mark.asyncio
async def test_check_report_get_latest_omits_absent_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["check_report_get_latest"].return_value = SimpleNamespace(
        to_dict=lambda: {"data": []}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check-report",
            "get-latest",
            "ri.data-health.main.check.x",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["check_report_get_latest"].assert_awaited_once_with(
        "ri.data-health.main.check.x", request_timeout=30
    )


@pytest.mark.asyncio
async def test_unknown_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "check", "list-all"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# JSON validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_config_json_rejected_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "check", "create", "--config-json", "not-json"],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_config_json_must_be_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "check", "create", "--config-json", "[1, 2]"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_config_json_required_for_create(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "check", "create"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


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
        [
            "check",
            "create",
            "--config-json",
            '{"type": "jobStatus", "subject": {"datasetRid": "ri.foundry.main.dataset.x", "branchId": "master"}, "statusCheckConfig": {"severity": "CRITICAL"}}',
        ],
        ["check", "delete", "ri.data-health.main.check.x"],
        [
            "check",
            "replace",
            "ri.data-health.main.check.x",
            "--config-json",
            '{"type": "jobStatus", "subject": {"datasetRid": "ri.foundry.main.dataset.x", "branchId": "master"}, "statusCheckConfig": {"severity": "WARNING"}}',
        ],
    ]
    for argv in writes:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 8
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_semantic_reads_permitted_under_readonly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.c"})
    calls["check_report_get_latest"].return_value = SimpleNamespace(
        to_dict=lambda: {"data": []}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "check", "get", "ri.data-health.main.check.x", "--format", "json"],
    )
    assert await cli.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check-report",
            "get-latest",
            "ri.data-health.main.check.x",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0


def test_metadata_only_permits_exactly_3_blocks_3() -> None:
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("check", "get"),
        ("check_report", "get"),
        ("check_report", "get_latest"),
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
    blocked = catalog - permitted
    assert blocked == {
        ("check", "create"),
        ("check", "delete"),
        ("check", "replace"),
    }


def test_metadata_only_runtime_permits_three_and_blocks_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    guard = AccessControlGuard(_Cfg(), "DATA_HEALTH")
    for resource, operation in [
        ("check", "create"),
        ("check", "delete"),
        ("check", "replace"),
    ]:
        with pytest.raises(AccessControlError):
            guard.check(resource, operation)
    for resource, operation in [
        ("check", "get"),
        ("check_report", "get"),
        ("check_report", "get_latest"),
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
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.c"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "check", "get", "ri.data-health.main.check.x", "--format", "json"],
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


def test_limit_accepts_1_to_100_bounds() -> None:
    assert cli._validate_limit(1) == 1
    assert cli._validate_limit(100) == 100
    for invalid in (0, 101, -1, True):
        with pytest.raises(cli.CLIInputError):
            cli._validate_limit(invalid)


def test_invalid_limit_message_never_echoes_value() -> None:
    sentinel = "limit-sentinel-999"
    with pytest.raises(cli.CLIInputError) as captured:
        cli._validate_limit(999)
    assert sentinel not in str(captured.value)
    assert str(captured.value) == "limit must be between 1 and 100"


@pytest.mark.asyncio
async def test_invalid_limit_stops_before_acl_or_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check-report",
            "get-latest",
            "ri.data-health.main.check.x",
            "--limit",
            "0",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert envelope["message"] == "limit must be between 1 and 100"
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_invalid_timeout_stops_before_acl_or_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "check",
            "get",
            "ri.data-health.main.check.x",
            "--timeout",
            "0",
        ],
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
        sys, "argv", ["cmd", "check", "get", "ri.data-health.main.check.x"]
    )
    assert await cli.main() == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 5


@pytest.mark.asyncio
async def test_output_toon_and_json_formats(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.c"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "check", "get", "ri.data-health.main.check.x", "--format", "toon"],
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
        sys, "argv", ["cmd", "check", "get", "ri.data-health.main.check.x"]
    )
    assert await cli.main() == 6
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message"] == "DataHealth operation failed"
    assert "secret-token" not in envelope["message"]
