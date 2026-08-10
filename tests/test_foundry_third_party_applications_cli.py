"""Tests for the 9-operation Foundry Third-Party Applications CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch,
the cursor-paged ``version list`` command through PaginationHelper, bounded
binary upload for ``version upload``/``version upload-snapshot``, access
control write classification (5 writes, 4 reads), read-only mode, the
packaged 4/9 metadata-only policy, attribution suppression, retry and error
taxonomy, timeouts, output formats, and the console boundary.

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
from foundry_cli.third_party_applications.scripts import (
    foundry_third_party_applications_cli as cli,
)


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


class _Page:
    def __init__(self, items: list[Any], next_page_token: str | None) -> None:
        self.data = items
        self.next_page_token = next_page_token

    def decode(self) -> "_Page":
        return self


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


def _root() -> tuple[Any, dict[str, AsyncMock]]:
    """Build a nested Third-Party Applications SDK fake."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    version_raw = _RawList([])
    version = SimpleNamespace(
        delete=tracked("version_delete"),
        get=tracked("version_get"),
        list=tracked("version_list"),
        upload=tracked("version_upload"),
        upload_snapshot=tracked("version_upload_snapshot"),
        with_raw_response=SimpleNamespace(list=version_raw),
    )
    website = SimpleNamespace(
        deploy=tracked("website_deploy"),
        get=tracked("website_get"),
        undeploy=tracked("website_undeploy"),
        Version=version,
    )
    third_party_application = SimpleNamespace(get=tracked("tpa_get"))
    root = SimpleNamespace(
        third_party_applications=SimpleNamespace(
            ThirdPartyApplication=third_party_application,
            Website=website,
        )
    )
    return root, calls


def _paged_root(pages: list[tuple[list[Any], str | None]]) -> tuple[Any, _RawList]:
    raw = _RawList(pages)
    version = SimpleNamespace(with_raw_response=SimpleNamespace(list=raw))
    website = SimpleNamespace(Version=version)
    root = SimpleNamespace(
        third_party_applications=SimpleNamespace(Website=website)
    )
    return root, raw


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_9_operations() -> None:
    assert len(cli.OP_SPECS) == 9
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 9
    assert len(cli.OPERATION_BY_RESOURCE) == 3
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("third_party_application", "get", ("ThirdPartyApplication",), "get"),
        ("version", "delete", ("Website", "Version"), "delete"),
        ("version", "get", ("Website", "Version"), "get"),
        ("version", "list", ("Website", "Version"), "list"),
        ("version", "upload", ("Website", "Version"), "upload"),
        ("version", "upload_snapshot", ("Website", "Version"), "upload_snapshot"),
        ("website", "deploy", ("Website",), "deploy"),
        ("website", "get", ("Website",), "get"),
        ("website", "undeploy", ("Website",), "undeploy"),
    ]


def test_catalog_marks_exactly_one_paginated_operation() -> None:
    assert cli.PAGINATED_OPS == frozenset({("version", "list")})


def test_all_write_set_ops_are_present_in_catalog() -> None:
    """Every 5-op write set and 4-op read set maps to a catalog entry."""
    write_pairs = {
        ("website", "deploy"),
        ("website", "undeploy"),
        ("version", "delete"),
        ("version", "upload"),
        ("version", "upload_snapshot"),
    }
    read_pairs = {
        ("third_party_application", "get"),
        ("website", "get"),
        ("version", "get"),
        ("version", "list"),
    }
    catalog_pairs = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    assert write_pairs | read_pairs == catalog_pairs
    assert len(write_pairs) == 5
    assert len(read_pairs) == 4


def test_upload_ops_declare_file_required() -> None:
    for pair in (("version", "upload"), ("version", "upload_snapshot")):
        spec = cli.OPERATION_BY_RESOURCE[pair[0]][pair[1]]
        assert "file" in spec["required"]
        assert "file" not in spec["optional"]


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(["third-party-application", "get", "ri.tpa"])
    _parse(["website", "deploy", "ri.tpa", "--version", "1.0.0"])
    _parse(["website", "get", "ri.tpa"])
    _parse(["website", "undeploy", "ri.tpa"])
    _parse(["version", "delete", "ri.tpa", "1.0.0"])
    _parse(["version", "get", "ri.tpa", "1.0.0"])
    _parse(
        [
            "version",
            "list",
            "ri.tpa",
            "--page-size",
            "10",
            "--page-token",
            "tok",
            "--all",
            "--max-pages",
            "5",
        ]
    )
    _parse(
        [
            "version",
            "upload",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            "build.zip",
        ]
    )
    _parse(
        [
            "version",
            "upload-snapshot",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            "build.zip",
            "--snapshot-identifier",
            "snap-1",
        ]
    )


def test_parser_rejects_unknown_operation() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["version", "list-all"])


def test_parser_rejects_missing_required_flag() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["website", "deploy", "ri.tpa"])


def test_non_paginated_commands_reject_pagination_flags() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["website", "get", "ri.tpa", "--page-size", "10"])


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_third_party_application_get_dispatches_exact_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["tpa_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.tpa"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "third-party-application", "get", "ri.tpa", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.tpa"}
    calls["tpa_get"].assert_awaited_once_with("ri.tpa", request_timeout=30)
    assert factory.create_calls == 1


@pytest.mark.asyncio
async def test_website_deploy_dispatches_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["website_deploy"].return_value = SimpleNamespace(
        to_dict=lambda: {"version": "1.0.0"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "website", "deploy", "ri.tpa", "--version", "1.0.0", "--format", "json"],
    )
    assert await cli.main() == 0
    calls["website_deploy"].assert_awaited_once_with(
        "ri.tpa", version="1.0.0", request_timeout=30
    )


@pytest.mark.asyncio
async def test_website_get_dispatches_and_omits_absent_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["website_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.tpa"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "website", "get", "ri.tpa", "--format", "json"]
    )
    assert await cli.main() == 0
    calls["website_get"].assert_awaited_once_with("ri.tpa", request_timeout=30)


@pytest.mark.asyncio
async def test_website_undeploy_dispatches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["website_undeploy"].return_value = SimpleNamespace(
        to_dict=lambda: {"undeployed": True}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "website", "undeploy", "ri.tpa", "--format", "json"]
    )
    assert await cli.main() == 0
    calls["website_undeploy"].assert_awaited_once_with("ri.tpa", request_timeout=30)


@pytest.mark.asyncio
async def test_version_delete_dispatches_positionals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["version_delete"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "version", "delete", "ri.tpa", "1.0.0", "--format", "json"],
    )
    assert await cli.main() == 0
    calls["version_delete"].assert_awaited_once_with("ri.tpa", "1.0.0", request_timeout=30)


@pytest.mark.asyncio
async def test_version_get_dispatches_positionals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["version_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"version": "1.0.0"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "version", "get", "ri.tpa", "1.0.0", "--format", "json"],
    )
    assert await cli.main() == 0
    calls["version_get"].assert_awaited_once_with("ri.tpa", "1.0.0", request_timeout=30)


@pytest.mark.asyncio
async def test_unknown_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "website", "list-all"])
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
    monkeypatch.setattr(sys, "argv", ["cmd", "website"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_required_value_rejected_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "website", "deploy", "ri.tpa", "--version", "  "],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Binary upload
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_upload_reads_file_bounded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, calls = _root()
    calls["version_upload"].return_value = SimpleNamespace(
        to_dict=lambda: {"version": "1.0.0"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    zip_file = tmp_path / "build.zip"
    zip_file.write_bytes(b"zip-bytes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "version",
            "upload",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            str(zip_file),
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["version_upload"].assert_awaited_once_with(
        "ri.tpa", b"zip-bytes", version="1.0.0", request_timeout=30
    )


@pytest.mark.asyncio
async def test_version_upload_snapshot_reads_file_bounded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, calls = _root()
    calls["version_upload_snapshot"].return_value = SimpleNamespace(
        to_dict=lambda: {"version": "1.0.0-snapshot"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    zip_file = tmp_path / "build.zip"
    zip_file.write_bytes(b"snap-bytes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "version",
            "upload-snapshot",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            str(zip_file),
            "--snapshot-identifier",
            "snap-1",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["version_upload_snapshot"].assert_awaited_once_with(
        "ri.tpa",
        b"snap-bytes",
        version="1.0.0",
        snapshot_identifier="snap-1",
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_version_upload_omits_absent_snapshot_identifier(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, calls = _root()
    calls["version_upload_snapshot"].return_value = SimpleNamespace(
        to_dict=lambda: {"version": "1.0.0-snapshot"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    zip_file = tmp_path / "build.zip"
    zip_file.write_bytes(b"snap-bytes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "version",
            "upload-snapshot",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            str(zip_file),
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["version_upload_snapshot"].assert_awaited_once_with(
        "ri.tpa", b"snap-bytes", version="1.0.0", request_timeout=30
    )


@pytest.mark.asyncio
async def test_version_upload_rejects_missing_file(
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
            "version",
            "upload",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            "no-such.zip",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_version_upload_rejects_oversized_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    big = tmp_path / "big.zip"
    big.write_bytes(b"x" * (cli._UPLOAD_MAX_BYTES + 1))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "version",
            "upload",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            str(big),
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_list_uses_raw_response_and_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, raw = _paged_root([(["1.0.0", "1.1.0"], "tok1"), (["1.2.0"], None)])
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "version", "list", "ri.tpa", "--max-pages", "5", "--format", "json"],
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == ["1.0.0", "1.1.0", "1.2.0"]
    assert len(raw.calls) == 2
    assert raw.calls[0]["page_token"] is None
    assert raw.calls[1]["page_token"] == "tok1"


@pytest.mark.asyncio
async def test_version_list_defaults_to_single_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, raw = _paged_root([(["x"], "tok2"), (["y"], None)])
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "version", "list", "ri.tpa", "--format", "json"]
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == ["x"]
    assert len(raw.calls) == 1


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_blocks_five_write_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    writes = [
        ["website", "deploy", "ri.tpa", "--version", "1.0.0"],
        ["website", "undeploy", "ri.tpa"],
        ["version", "delete", "ri.tpa", "1.0.0"],
        ["version", "upload", "ri.tpa", "--version", "1.0.0", "--file", "a.zip"],
        [
            "version",
            "upload-snapshot",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            "a.zip",
        ],
    ]
    for argv in writes:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 8
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_reads_permitted_under_readonly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    root, calls = _root()
    calls["tpa_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.tpa"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "third-party-application", "get", "ri.tpa", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.tpa"}
    assert factory.create_calls == 1


def test_acl_write_classification_matches_design() -> None:
    """AccessControlGuard classifies the 5-op write set as writes."""
    guard = AccessControlGuard(_Cfg(), "THIRD_PARTY_APPLICATIONS")
    for pair in [
        ("website", "deploy"),
        ("website", "undeploy"),
        ("version", "delete"),
        ("version", "upload"),
        ("version", "upload_snapshot"),
    ]:
        assert guard._is_write_operation(pair[1]) is True, pair
    for pair in [
        ("third_party_application", "get"),
        ("website", "get"),
        ("version", "get"),
        ("version", "list"),
    ]:
        assert guard._is_write_operation(pair[1]) is False, pair


def test_deploy_and_undeploy_verbs_registered_globally() -> None:
    """deploy/undeploy are write verbs across all namespaces."""
    guard = AccessControlGuard(_Cfg(), "THIRD_PARTY_APPLICATIONS")
    assert guard._is_write_operation("deploy") is True
    assert guard._is_write_operation("undeploy") is True


# --------------------------------------------------------------------------
# Metadata-only policy
# --------------------------------------------------------------------------


def test_metadata_only_permits_exactly_4_blocks_5() -> None:
    """The packaged allow-list permits exactly 4 of 9 operations."""
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("third_party_application", "get"),
        ("website", "get"),
        ("version", "get"),
        ("version", "list"),
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
        ("website", "deploy"),
        ("website", "undeploy"),
        ("version", "delete"),
        ("version", "upload"),
        ("version", "upload_snapshot"),
    }


@pytest.mark.asyncio
async def test_metadata_only_permits_four_and_blocks_five(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    root, calls = _root()
    calls["tpa_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.tpa"}
    )
    calls["website_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.tpa"}
    )
    calls["version_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"version": "1.0.0"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    permitted = [
        ["third-party-application", "get", "ri.tpa"],
        ["website", "get", "ri.tpa"],
        ["version", "get", "ri.tpa", "1.0.0"],
        ["version", "list", "ri.tpa"],
    ]
    for argv in permitted:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 0, argv
    capsys.readouterr()  # Discard permitted-run output before blocked checks.
    blocked = [
        ["website", "deploy", "ri.tpa", "--version", "1.0.0"],
        ["website", "undeploy", "ri.tpa"],
        ["version", "delete", "ri.tpa", "1.0.0"],
        ["version", "upload", "ri.tpa", "--version", "1.0.0", "--file", "a.zip"],
        [
            "version",
            "upload-snapshot",
            "ri.tpa",
            "--version",
            "1.0.0",
            "--file",
            "a.zip",
        ],
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
    calls["tpa_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.tpa"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "third-party-application", "get", "ri.tpa", "--format", "json"],
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
async def test_invalid_timeout_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "website", "get", "ri.tpa", "--timeout", "0"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


@pytest.mark.asyncio
async def test_sdk_error_maps_to_server_error_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["website_get"].side_effect = RuntimeError("boom")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "website", "get", "ri.tpa", "--format", "json"]
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
    calls["website_get"].side_effect = TimeoutError("slow")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "website", "get", "ri.tpa", "--format", "json"]
    )
    assert await cli.main() == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 5


@pytest.mark.asyncio
async def test_toon_output_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["tpa_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.tpa"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "third-party-application", "get", "ri.tpa", "--format", "toon"],
    )
    assert await cli.main() == 0
    assert "rid" in capsys.readouterr().out


def test_console_main_wraps_async_entry() -> None:
    with patch.object(cli, "asyncio") as mock_asyncio:
        mock_asyncio.run.return_value = 0
        assert cli.console_main() == 0
        mock_asyncio.run.assert_called_once()
