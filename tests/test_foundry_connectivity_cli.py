"""Tests for the 20-operation Foundry Connectivity CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch,
JSON validation, the cursor-paged file-import/table-import list commands
through PaginationHelper, bounded binary upload for
upload-custom-jdbc-drivers (with .jar validation), access control write
classification (13 writes, get_configuration_batch semantic read), read-only
mode, the packaged 7/13 metadata-only policy, attribution suppression,
retry and error taxonomy, timeouts, output formats, secrets privacy, and
the console boundary.

All SDK transport is mocked: no live Foundry connection is ever made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foundry_cli.common.access_control_guard import AccessControlGuard
from foundry_cli.connectivity.scripts import foundry_connectivity_cli as cli


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

    def get_int(self, name: str, default: int | None = None) -> int | None:
        return None


def _root() -> tuple[Any, dict[str, AsyncMock]]:
    """Build a nested Connectivity SDK fake rooted at client.connectivity."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    virtual_table = SimpleNamespace(create=tracked("virtual_table_create"))
    table_import_raw = _RawList([])
    file_import_raw = _RawList([])
    table_import = SimpleNamespace(
        create=tracked("table_import_create"),
        delete=tracked("table_import_delete"),
        execute=tracked("table_import_execute"),
        get=tracked("table_import_get"),
        list=tracked("table_import_list"),
        replace=tracked("table_import_replace"),
        with_raw_response=SimpleNamespace(list=table_import_raw),
    )
    file_import = SimpleNamespace(
        create=tracked("file_import_create"),
        delete=tracked("file_import_delete"),
        execute=tracked("file_import_execute"),
        get=tracked("file_import_get"),
        list=tracked("file_import_list"),
        replace=tracked("file_import_replace"),
        with_raw_response=SimpleNamespace(list=file_import_raw),
    )
    connection = SimpleNamespace(
        create=tracked("connection_create"),
        get=tracked("connection_get"),
        get_configuration=tracked("connection_get_configuration"),
        get_configuration_batch=tracked("connection_get_configuration_batch"),
        update_export_settings=tracked("connection_update_export_settings"),
        update_secrets=tracked("connection_update_secrets"),
        upload_custom_jdbc_drivers=tracked("connection_upload_custom_jdbc_drivers"),
        FileImport=file_import,
        TableImport=table_import,
        VirtualTable=virtual_table,
    )
    root = SimpleNamespace(connectivity=SimpleNamespace(Connection=connection))
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_20_operations() -> None:
    assert len(cli.OP_SPECS) == 20
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 20
    assert len(cli.OPERATION_BY_RESOURCE) == 4
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("connection", "create", ("Connection",), "create"),
        ("connection", "get", ("Connection",), "get"),
        ("connection", "get_configuration", ("Connection",), "get_configuration"),
        (
            "connection",
            "get_configuration_batch",
            ("Connection",),
            "get_configuration_batch",
        ),
        (
            "connection",
            "update_export_settings",
            ("Connection",),
            "update_export_settings",
        ),
        ("connection", "update_secrets", ("Connection",), "update_secrets"),
        (
            "connection",
            "upload_custom_jdbc_drivers",
            ("Connection",),
            "upload_custom_jdbc_drivers",
        ),
        ("file_import", "create", ("Connection", "FileImport"), "create"),
        ("file_import", "delete", ("Connection", "FileImport"), "delete"),
        ("file_import", "execute", ("Connection", "FileImport"), "execute"),
        ("file_import", "get", ("Connection", "FileImport"), "get"),
        ("file_import", "list", ("Connection", "FileImport"), "list"),
        ("file_import", "replace", ("Connection", "FileImport"), "replace"),
        ("table_import", "create", ("Connection", "TableImport"), "create"),
        ("table_import", "delete", ("Connection", "TableImport"), "delete"),
        ("table_import", "execute", ("Connection", "TableImport"), "execute"),
        ("table_import", "get", ("Connection", "TableImport"), "get"),
        ("table_import", "list", ("Connection", "TableImport"), "list"),
        ("table_import", "replace", ("Connection", "TableImport"), "replace"),
        ("virtual_table", "create", ("Connection", "VirtualTable"), "create"),
    ]


def test_catalog_marks_exactly_two_paginated_operations() -> None:
    assert cli.PAGINATED_OPS == frozenset(
        {
            ("file_import", "list"),
            ("table_import", "list"),
        }
    )


def test_all_write_set_ops_are_present_in_catalog() -> None:
    """Every 13-op write set and 7-op read set maps to a catalog entry."""
    write_pairs = {
        ("connection", "create"),
        ("connection", "update_export_settings"),
        ("connection", "update_secrets"),
        ("connection", "upload_custom_jdbc_drivers"),
        ("file_import", "create"),
        ("file_import", "delete"),
        ("file_import", "execute"),
        ("file_import", "replace"),
        ("table_import", "create"),
        ("table_import", "delete"),
        ("table_import", "execute"),
        ("table_import", "replace"),
        ("virtual_table", "create"),
    }
    read_pairs = {
        ("connection", "get"),
        ("connection", "get_configuration"),
        ("connection", "get_configuration_batch"),
        ("file_import", "get"),
        ("file_import", "list"),
        ("table_import", "get"),
        ("table_import", "list"),
    }
    catalog_pairs = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    assert write_pairs | read_pairs == catalog_pairs
    assert len(write_pairs) == 13
    assert len(read_pairs) == 7


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(
        [
            "connection",
            "create",
            "--configuration-json",
            "{}",
            "--display-name",
            "src",
            "--parent-folder-rid",
            "ri.folder",
            "--worker-json",
            "{}",
        ]
    )
    _parse(["connection", "get", "ri.conn"])
    _parse(["connection", "get-configuration", "ri.conn"])
    _parse(
        ["connection", "get-configuration-batch", "--body-json", "[{\"connectionRid\": \"ri.conn\"}]"]
    )
    _parse(
        [
            "connection",
            "update-export-settings",
            "ri.conn",
            "--export-settings-json",
            "{}",
        ]
    )
    _parse(["connection", "update-secrets", "ri.conn", "--secrets-json", "{}"])
    _parse(
        [
            "connection",
            "upload-custom-jdbc-drivers",
            "ri.conn",
            "--file",
            "drivers.jar",
            "--file-name",
            "drivers.jar",
        ]
    )
    _parse(
        [
            "file-import",
            "create",
            "ri.conn",
            "--dataset-rid",
            "ri.dataset",
            "--display-name",
            "imp",
            "--filters-json",
            "[]",
            "--import-mode",
            "SNAPSHOT",
            "--branch-name",
            "master",
            "--subfolder",
            "sub",
        ]
    )
    _parse(["file-import", "delete", "ri.conn", "ri.fi"])
    _parse(["file-import", "execute", "ri.conn", "ri.fi"])
    _parse(["file-import", "get", "ri.conn", "ri.fi"])
    _parse(
        ["file-import", "list", "ri.conn", "--page-size", "10", "--page-token", "tok"]
    )
    _parse(
        [
            "file-import",
            "replace",
            "ri.conn",
            "ri.fi",
            "--display-name",
            "imp",
            "--filters-json",
            "[]",
            "--import-mode",
            "SNAPSHOT",
        ]
    )
    _parse(
        [
            "table-import",
            "create",
            "ri.conn",
            "--config-json",
            "{}",
            "--dataset-rid",
            "ri.dataset",
            "--display-name",
            "t",
            "--import-mode",
            "SNAPSHOT",
            "--allow-schema-changes",
            "--branch-name",
            "master",
        ]
    )
    _parse(["table-import", "delete", "ri.conn", "ri.ti"])
    _parse(["table-import", "execute", "ri.conn", "ri.ti"])
    _parse(["table-import", "get", "ri.conn", "ri.ti"])
    _parse(
        ["table-import", "list", "ri.conn", "--page-size", "10", "--page-token", "tok"]
    )
    _parse(
        [
            "table-import",
            "replace",
            "ri.conn",
            "ri.ti",
            "--config-json",
            "{}",
            "--display-name",
            "t",
            "--import-mode",
            "SNAPSHOT",
        ]
    )
    _parse(
        [
            "virtual-table",
            "create",
            "ri.conn",
            "--config-json",
            "{}",
            "--name",
            "vt",
            "--parent-rid",
            "ri.folder",
            "--markings-json",
            '["m1"]',
        ]
    )


def test_parser_rejects_unknown_operation() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["connection", "list-all"])


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_create_dispatches_exact_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_create"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.conn"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "connection",
            "create",
            "--configuration-json",
            '{"type": "jdbc"}',
            "--display-name",
            "src",
            "--parent-folder-rid",
            "ri.folder",
            "--worker-json",
            "{}",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.conn"}
    calls["connection_create"].assert_awaited_once_with(
        configuration={"type": "jdbc"},
        display_name="src",
        parent_folder_rid="ri.folder",
        worker={},
        request_timeout=30,
    )
    assert factory.create_calls == 1


@pytest.mark.asyncio
async def test_connection_get_configuration_batch_is_semantic_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_get_configuration_batch"].return_value = SimpleNamespace(
        to_dict=lambda: {"configurations": {}}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "connection",
            "get-configuration-batch",
            "--body-json",
            '[{"connectionRid": "ri.conn"}]',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"configurations": {}}
    calls["connection_get_configuration_batch"].assert_awaited_once_with(
        body=[{"connectionRid": "ri.conn"}], request_timeout=30
    )


@pytest.mark.asyncio
async def test_connection_get_dispatches_and_omits_absent_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.conn"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "connection", "get", "ri.conn", "--format", "json"]
    )
    assert await cli.main() == 0
    calls["connection_get"].assert_awaited_once_with("ri.conn", request_timeout=30)


@pytest.mark.asyncio
async def test_file_import_create_dispatches_with_filters_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["file_import_create"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.fi"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "file-import",
            "create",
            "ri.conn",
            "--dataset-rid",
            "ri.dataset",
            "--display-name",
            "imp",
            "--filters-json",
            '[{"type": "fileSizeFilter", "gt": 1}]',
            "--import-mode",
            "SNAPSHOT",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["file_import_create"].assert_awaited_once_with(
        "ri.conn",
        dataset_rid="ri.dataset",
        display_name="imp",
        file_import_filters=[{"type": "fileSizeFilter", "gt": 1}],
        import_mode="SNAPSHOT",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_file_import_replace_dispatches_with_filters_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["file_import_replace"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.fi"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "file-import",
            "replace",
            "ri.conn",
            "ri.fi",
            "--display-name",
            "imp",
            "--filters-json",
            '[{"type": "fileSizeFilter", "lt": 10}]',
            "--import-mode",
            "SNAPSHOT",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["file_import_replace"].assert_awaited_once_with(
        "ri.conn",
        "ri.fi",
        display_name="imp",
        file_import_filters=[{"type": "fileSizeFilter", "lt": 10}],
        import_mode="SNAPSHOT",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_table_import_replace_omits_absent_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["table_import_replace"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.ti"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "table-import",
            "replace",
            "ri.conn",
            "ri.ti",
            "--config-json",
            "{}",
            "--display-name",
            "t",
            "--import-mode",
            "SNAPSHOT",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["table_import_replace"].assert_awaited_once_with(
        "ri.conn",
        "ri.ti",
        config={},
        display_name="t",
        import_mode="SNAPSHOT",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_virtual_table_create_dispatches_with_markings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["virtual_table_create"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.vt"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "virtual-table",
            "create",
            "ri.conn",
            "--config-json",
            "{}",
            "--name",
            "vt",
            "--parent-rid",
            "ri.folder",
            "--markings-json",
            '["m1"]',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["virtual_table_create"].assert_awaited_once_with(
        "ri.conn",
        config={},
        name="vt",
        parent_rid="ri.folder",
        markings=["m1"],
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_unknown_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "connection", "list-all"])
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
    monkeypatch.setattr(sys, "argv", ["cmd", "connection"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# JSON validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_configuration_json_rejected_before_client(
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
            "connection",
            "create",
            "--configuration-json",
            "not-json",
            "--display-name",
            "src",
            "--parent-folder-rid",
            "ri.folder",
            "--worker-json",
            "{}",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_filters_json_array_required_for_file_import(
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
            "file-import",
            "create",
            "ri.conn",
            "--dataset-rid",
            "ri.dataset",
            "--display-name",
            "imp",
            "--filters-json",
            "{}",
            "--import-mode",
            "SNAPSHOT",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Binary upload
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_custom_jdbc_drivers_reads_file_bounded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, calls = _root()
    calls["connection_upload_custom_jdbc_drivers"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.conn"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    jar = tmp_path / "drivers.jar"
    jar.write_bytes(b"jdbc-bytes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "connection",
            "upload-custom-jdbc-drivers",
            "ri.conn",
            "--file",
            str(jar),
            "--file-name",
            "drivers.jar",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["connection_upload_custom_jdbc_drivers"].assert_awaited_once_with(
        "ri.conn", b"jdbc-bytes", file_name="drivers.jar", request_timeout=30
    )


@pytest.mark.asyncio
async def test_upload_custom_jdbc_drivers_rejects_non_jar(
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
            "connection",
            "upload-custom-jdbc-drivers",
            "ri.conn",
            "--file",
            "drivers.zip",
            "--file-name",
            "drivers.zip",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_upload_custom_jdbc_drivers_rejects_missing_file(
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
            "connection",
            "upload-custom-jdbc-drivers",
            "ri.conn",
            "--file",
            "no-such.jar",
            "--file-name",
            "no-such.jar",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


class _RawList:
    """Decoded page response double for cursor-paged list operations."""

    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.pages:
            return _Page([], None)
        items, nxt = self.pages[min(len(self.calls) - 1, len(self.pages) - 1)]
        return _Page(items, nxt)


class _Page:
    def __init__(self, items: list[Any], next_page_token: str | None) -> None:
        self.data = items
        self.next_page_token = next_page_token

    def decode(self) -> "_Page":
        return self


class _RawFileImport:
    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.list = _RawList(pages)
        self.get = _RawList([])


class _RawTableImport:
    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.list = _RawList(pages)


def _paged_root(file_import_pages: Any, table_import_pages: Any) -> Any:
    fi_raw = _RawFileImport(file_import_pages)
    ti_raw = _RawTableImport(table_import_pages)
    file_import = SimpleNamespace(with_raw_response=fi_raw)
    table_import = SimpleNamespace(with_raw_response=ti_raw)
    connection = SimpleNamespace(FileImport=file_import, TableImport=table_import)
    return SimpleNamespace(connectivity=SimpleNamespace(Connection=connection)), fi_raw, ti_raw


@pytest.mark.asyncio
async def test_file_import_list_uses_raw_response_and_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, fi_raw, _ = _paged_root(
        [(["a", "b"], "tok1"), (["c"], None)], []
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "file-import", "list", "ri.conn", "--max-pages", "5", "--format", "json"],
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == ["a", "b", "c"]
    assert len(fi_raw.list.calls) == 2
    assert fi_raw.list.calls[0]["page_token"] is None
    assert fi_raw.list.calls[1]["page_token"] == "tok1"


@pytest.mark.asyncio
async def test_table_import_list_defaults_to_single_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, ti_raw = _paged_root([], [(["x"], "tok2"), (["y"], None)])
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "table-import", "list", "ri.conn", "--format", "json"]
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == ["x"]
    assert len(ti_raw.list.calls) == 1


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_blocks_thirteen_write_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    writes = [
        ["connection", "create", "--configuration-json", "{}", "--display-name", "x", "--parent-folder-rid", "r", "--worker-json", "{}"],
        ["connection", "update-export-settings", "ri.conn", "--export-settings-json", "{}"],
        ["connection", "update-secrets", "ri.conn", "--secrets-json", "{}"],
        ["connection", "upload-custom-jdbc-drivers", "ri.conn", "--file", "a.jar", "--file-name", "a.jar"],
        ["file-import", "create", "ri.conn", "--dataset-rid", "d", "--display-name", "i", "--filters-json", "[]", "--import-mode", "SNAPSHOT"],
        ["file-import", "delete", "ri.conn", "ri.fi"],
        ["file-import", "execute", "ri.conn", "ri.fi"],
        ["file-import", "replace", "ri.conn", "ri.fi", "--display-name", "i", "--filters-json", "[]", "--import-mode", "SNAPSHOT"],
        ["table-import", "create", "ri.conn", "--config-json", "{}", "--dataset-rid", "d", "--display-name", "t", "--import-mode", "SNAPSHOT"],
        ["table-import", "delete", "ri.conn", "ri.ti"],
        ["table-import", "execute", "ri.conn", "ri.ti"],
        ["table-import", "replace", "ri.conn", "ri.ti", "--config-json", "{}", "--display-name", "t", "--import-mode", "SNAPSHOT"],
        ["virtual-table", "create", "ri.conn", "--config-json", "{}", "--name", "v", "--parent-rid", "r"],
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
    calls["connection_get_configuration_batch"].return_value = SimpleNamespace(
        to_dict=lambda: {"configurations": {}}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "connection",
            "get-configuration-batch",
            "--body-json",
            '[{"connectionRid": "ri.conn"}]',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"configurations": {}}
    assert factory.create_calls == 1


def test_acl_write_classification_matches_design() -> None:
    """AccessControlGuard classifies the 13-op write set as writes."""
    guard = AccessControlGuard(_Cfg(), "CONNECTIVITY")
    for pair in [
        ("connection", "create"),
        ("connection", "update_export_settings"),
        ("connection", "update_secrets"),
        ("connection", "upload_custom_jdbc_drivers"),
        ("file_import", "create"),
        ("file_import", "delete"),
        ("file_import", "execute"),
        ("file_import", "replace"),
        ("table_import", "create"),
        ("table_import", "delete"),
        ("table_import", "execute"),
        ("table_import", "replace"),
        ("virtual_table", "create"),
    ]:
        assert guard._is_write_operation(pair[1]) is True, pair
    for pair in [
        ("connection", "get"),
        ("connection", "get_configuration"),
        ("connection", "get_configuration_batch"),
        ("file_import", "get"),
        ("file_import", "list"),
        ("table_import", "get"),
        ("table_import", "list"),
    ]:
        assert guard._is_write_operation(pair[1]) is False, pair


# --------------------------------------------------------------------------
# Metadata-only policy
# --------------------------------------------------------------------------


def test_metadata_only_permits_exactly_7_blocks_13() -> None:
    """The packaged allow-list permits exactly 7 of 20 operations."""
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("connection", "get"),
        ("connection", "get_configuration"),
        ("connection", "get_configuration_batch"),
        ("file_import", "get"),
        ("file_import", "list"),
        ("table_import", "get"),
        ("table_import", "list"),
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
        ("connection", "create"),
        ("connection", "update_export_settings"),
        ("connection", "update_secrets"),
        ("connection", "upload_custom_jdbc_drivers"),
        ("file_import", "create"),
        ("file_import", "delete"),
        ("file_import", "execute"),
        ("file_import", "replace"),
        ("table_import", "create"),
        ("table_import", "delete"),
        ("table_import", "execute"),
        ("table_import", "replace"),
        ("virtual_table", "create"),
    }


@pytest.mark.asyncio
async def test_metadata_only_permits_seven_and_blocks_thirteen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    root, calls = _root()
    calls["connection_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.conn"}
    )
    calls["connection_get_configuration_batch"].return_value = SimpleNamespace(
        to_dict=lambda: {"configurations": {}}
    )
    calls["file_import_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.fi"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    permitted = [
        ["connection", "get", "ri.conn"],
        ["connection", "get-configuration", "ri.conn"],
        ["connection", "get-configuration-batch", "--body-json", "[]"],
        ["file-import", "get", "ri.conn", "ri.fi"],
        ["file-import", "list", "ri.conn"],
        ["table-import", "get", "ri.conn", "ri.ti"],
        ["table-import", "list", "ri.conn"],
    ]
    for argv in permitted:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 0, argv
    capsys.readouterr()  # Discard permitted-run output before blocked checks.
    blocked = [
        ["connection", "create", "--configuration-json", "{}", "--display-name", "x", "--parent-folder-rid", "r", "--worker-json", "{}"],
        ["connection", "update-export-settings", "ri.conn", "--export-settings-json", "{}"],
        ["connection", "update-secrets", "ri.conn", "--secrets-json", "{}"],
        ["connection", "upload-custom-jdbc-drivers", "ri.conn", "--file", "a.jar", "--file-name", "a.jar"],
        ["file-import", "create", "ri.conn", "--dataset-rid", "d", "--display-name", "i", "--filters-json", "[]", "--import-mode", "SNAPSHOT"],
        ["file-import", "delete", "ri.conn", "ri.fi"],
        ["file-import", "execute", "ri.conn", "ri.fi"],
        ["file-import", "replace", "ri.conn", "ri.fi", "--display-name", "i", "--filters-json", "[]", "--import-mode", "SNAPSHOT"],
        ["table-import", "create", "ri.conn", "--config-json", "{}", "--dataset-rid", "d", "--display-name", "t", "--import-mode", "SNAPSHOT"],
        ["table-import", "delete", "ri.conn", "ri.ti"],
        ["table-import", "execute", "ri.conn", "ri.ti"],
        ["table-import", "replace", "ri.conn", "ri.ti", "--config-json", "{}", "--display-name", "t", "--import-mode", "SNAPSHOT"],
        ["virtual-table", "create", "ri.conn", "--config-json", "{}", "--name", "v", "--parent-rid", "r"],
    ]
    for argv in blocked:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8, argv
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 8, argv


# --------------------------------------------------------------------------
# Attribution suppression
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_uses_include_attribution_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.conn"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "connection", "get", "ri.conn", "--format", "json"]
    )
    assert await cli.main() == 0
    assert factory.scope_kwargs == {"include_attribution": False}
    assert factory.create_kwargs == {"include_attribution": False}


# --------------------------------------------------------------------------
# Secrets privacy
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_secrets_never_echoes_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_update_secrets"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "connection",
            "update-secrets",
            "ri.conn",
            "--secrets-json",
            '{"db_password": "super-secret-value"}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert "super-secret-value" not in out
    calls["connection_update_secrets"].assert_awaited_once_with(
        "ri.conn",
        secrets={"db_password": "super-secret-value"},
        request_timeout=30,
    )


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
async def test_invalid_timeout_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "connection", "get", "ri.conn", "--timeout", "0"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


@pytest.mark.asyncio
async def test_sdk_error_maps_to_server_error_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_get"].side_effect = RuntimeError("boom")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "connection", "get", "ri.conn", "--format", "json"]
    )
    assert await cli.main() == 6
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 6
    assert "boom" not in envelope["message"]


@pytest.mark.asyncio
async def test_sdk_timeout_maps_to_timeout_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_get"].side_effect = TimeoutError("slow")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "connection", "get", "ri.conn", "--format", "json"]
    )
    assert await cli.main() == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 5


@pytest.mark.asyncio
async def test_toon_output_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["connection_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.conn"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "connection", "get", "ri.conn", "--format", "toon"],
    )
    assert await cli.main() == 0
    assert "rid" in capsys.readouterr().out


def test_console_main_wraps_async_entry() -> None:
    with patch.object(cli, "asyncio") as mock_asyncio:
        mock_asyncio.run.return_value = 0
        assert cli.console_main() == 0
        mock_asyncio.run.assert_called_once()
