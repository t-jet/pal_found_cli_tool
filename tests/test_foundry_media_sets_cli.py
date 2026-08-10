"""Tests for the 19-operation Foundry Media Sets CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch,
JSON validation, the four streamed binary downloads through
BinaryDownloadHandler (bounded truncation, FR-DL envelope fields, unsafe
filename rejection), the two bounded binary uploads, access control write
classification (9 writes, content reads blocked under metadata-only),
read-only mode, the packaged 5/14 metadata-only policy, attribution
(include_attribution=True per FR-ATTR-4, prior state restored after
success and failure), retry and error taxonomy, timeouts, output formats,
privacy, and the console boundary.

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
from foundry_cli.common.async_client_factory import AsyncClientFactory
from foundry_cli.media_sets.scripts import foundry_media_sets_cli as cli


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
    """Build a nested MediaSets SDK fake rooted at client.media_sets."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    media_set = SimpleNamespace(
        abort=tracked("abort"),
        calculate=tracked("calculate"),
        clear=tracked("clear"),
        commit=tracked("commit"),
        create=tracked("create"),
        get=tracked("get"),
        get_result=tracked("get_result"),
        get_rid_by_path=tracked("get_rid_by_path"),
        get_status=tracked("get_status"),
        info=tracked("info"),
        metadata=tracked("metadata"),
        read=tracked("read"),
        read_original=tracked("read_original"),
        reference=tracked("reference"),
        register=tracked("register"),
        retrieve=tracked("retrieve"),
        transform=tracked("transform"),
        upload=tracked("upload"),
        upload_media=tracked("upload_media"),
    )
    root = SimpleNamespace(media_sets=SimpleNamespace(MediaSet=media_set))
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_19_operations() -> None:
    assert len(cli.OP_SPECS) == 19
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 19
    assert len(cli.OPERATION_BY_RESOURCE) == 1
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("media_set", "abort", ("MediaSet",), "abort"),
        ("media_set", "calculate", ("MediaSet",), "calculate"),
        ("media_set", "clear", ("MediaSet",), "clear"),
        ("media_set", "commit", ("MediaSet",), "commit"),
        ("media_set", "create", ("MediaSet",), "create"),
        ("media_set", "get", ("MediaSet",), "get"),
        ("media_set", "get_result", ("MediaSet",), "get_result"),
        ("media_set", "get_rid_by_path", ("MediaSet",), "get_rid_by_path"),
        ("media_set", "get_status", ("MediaSet",), "get_status"),
        ("media_set", "info", ("MediaSet",), "info"),
        ("media_set", "metadata", ("MediaSet",), "metadata"),
        ("media_set", "read", ("MediaSet",), "read"),
        ("media_set", "read_original", ("MediaSet",), "read_original"),
        ("media_set", "reference", ("MediaSet",), "reference"),
        ("media_set", "register", ("MediaSet",), "register"),
        ("media_set", "retrieve", ("MediaSet",), "retrieve"),
        ("media_set", "transform", ("MediaSet",), "transform"),
        ("media_set", "upload", ("MediaSet",), "upload"),
        ("media_set", "upload_media", ("MediaSet",), "upload_media"),
    ]


def test_catalog_marks_downloads_and_uploads() -> None:
    assert cli.DOWNLOAD_OPS == frozenset(
        {
            ("media_set", "get_result"),
            ("media_set", "read"),
            ("media_set", "read_original"),
            ("media_set", "retrieve"),
        }
    )
    assert cli._UPLOAD_OPS == frozenset(
        {
            ("media_set", "upload"),
            ("media_set", "upload_media"),
        }
    )


def test_write_and_read_sets_map_to_catalog() -> None:
    write_pairs = {
        ("media_set", "abort"),
        ("media_set", "calculate"),
        ("media_set", "clear"),
        ("media_set", "commit"),
        ("media_set", "create"),
        ("media_set", "register"),
        ("media_set", "transform"),
        ("media_set", "upload"),
        ("media_set", "upload_media"),
    }
    read_pairs = {
        ("media_set", "get"),
        ("media_set", "get_result"),
        ("media_set", "get_rid_by_path"),
        ("media_set", "get_status"),
        ("media_set", "info"),
        ("media_set", "metadata"),
        ("media_set", "read"),
        ("media_set", "read_original"),
        ("media_set", "reference"),
        ("media_set", "retrieve"),
    }
    catalog_pairs = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    assert write_pairs | read_pairs == catalog_pairs
    assert len(write_pairs) == 9
    assert len(read_pairs) == 10


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(["media-set", "abort", "ri.ms", "t1", "--preview"])
    _parse(["media-set", "calculate", "ri.ms", "ri.mi", "--read-token", "tok"])
    _parse(
        [
            "media-set",
            "clear",
            "ri.ms",
            "--media-item-path",
            "path",
            "--transaction-id",
            "t1",
        ]
    )
    _parse(["media-set", "commit", "ri.ms", "t1"])
    _parse(["media-set", "create", "ri.ms", "--branch-name", "master"])
    _parse(["media-set", "get", "ri.ms"])
    _parse(
        [
            "media-set",
            "get-result",
            "ri.ms",
            "ri.mi",
            "job1",
            "--output",
            "result.bin",
            "--token",
            "tok",
        ]
    )
    _parse(
        ["media-set", "get-rid-by-path", "ri.ms", "--media-item-path", "path"]
    )
    _parse(["media-set", "get-status", "ri.ms", "ri.mi", "job1"])
    _parse(["media-set", "info", "ri.ms", "ri.mi"])
    _parse(["media-set", "metadata", "ri.ms", "ri.mi"])
    _parse(["media-set", "read", "ri.ms", "ri.mi", "--output", "out.bin"])
    _parse(["media-set", "read-original", "ri.ms", "ri.mi", "--output", "out.bin"])
    _parse(["media-set", "reference", "ri.ms", "ri.mi"])
    _parse(
        [
            "media-set",
            "register",
            "ri.ms",
            "--physical-item-name",
            "item",
            "--transaction-id",
            "t1",
        ]
    )
    _parse(["media-set", "retrieve", "ri.ms", "ri.mi", "--output", "thumb.webp"])
    _parse(
        [
            "media-set",
            "transform",
            "ri.ms",
            "ri.mi",
            "--transformation-json",
            "{}",
            "--token",
            "tok",
        ]
    )
    _parse(
        [
            "media-set",
            "upload",
            "ri.ms",
            "--file",
            "item.bin",
            "--transaction-id",
            "t1",
        ]
    )
    _parse(["media-set", "upload-media", "--file", "item.bin", "--filename", "item.bin"])


def test_parser_rejects_unknown_operation() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["media-set", "list-all"])


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_set_get_dispatches_exact_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.ms"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "media-set", "get", "ri.ms", "--format", "json"]
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.ms"}
    calls["get"].assert_awaited_once_with("ri.ms", request_timeout=30)
    assert factory.create_calls == 1


@pytest.mark.asyncio
async def test_transform_dispatches_with_json_transformation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["transform"].return_value = SimpleNamespace(
        to_dict=lambda: {"job_id": "job1"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "media-set",
            "transform",
            "ri.ms",
            "ri.mi",
            "--transformation-json",
            '{"type": "imagery:thumbnail"}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["transform"].assert_awaited_once_with(
        "ri.ms",
        "ri.mi",
        transformation={"type": "imagery:thumbnail"},
        request_timeout=30,
    )


@pytest.mark.asyncio
async def test_transaction_lifecycle_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["create"].return_value = SimpleNamespace(to_dict=lambda: {"transaction_id": "t1"})
    calls["commit"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "media-set", "create", "ri.ms", "--format", "json"]
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"transaction_id": "t1"}
    calls["create"].assert_awaited_once_with("ri.ms", request_timeout=30)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "media-set", "commit", "ri.ms", "t1", "--format", "json"]
    )
    assert await cli.main() == 0
    calls["commit"].assert_awaited_once_with("ri.ms", "t1", request_timeout=30)


@pytest.mark.asyncio
async def test_upload_reads_file_bounded(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, calls = _root()
    calls["upload"].return_value = SimpleNamespace(to_dict=lambda: {"media_item_rid": "ri.mi"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    item = tmp_path / "item.bin"
    item.write_bytes(b"media-bytes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "media-set",
            "upload",
            "ri.ms",
            "--file",
            str(item),
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["upload"].assert_awaited_once_with(
        "ri.ms", b"media-bytes", request_timeout=30
    )


@pytest.mark.asyncio
async def test_upload_media_dispatches_with_filename(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root, calls = _root()
    calls["upload_media"].return_value = SimpleNamespace(
        to_dict=lambda: {"media_reference": "ref"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    item = tmp_path / "temp.bin"
    item.write_bytes(b"tmp-bytes")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "media-set",
            "upload-media",
            "--file",
            str(item),
            "--filename",
            "temp.bin",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["upload_media"].assert_awaited_once_with(
        b"tmp-bytes", filename="temp.bin", request_timeout=30
    )


@pytest.mark.asyncio
async def test_upload_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "media-set", "upload", "ri.ms", "--file", "no-such.bin"],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_unknown_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "media-set", "list-all"])
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
    monkeypatch.setattr(sys, "argv", ["cmd", "media-set"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# JSON validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_transformation_json_rejected_before_client(
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
            "media-set",
            "transform",
            "ri.ms",
            "ri.mi",
            "--transformation-json",
            "not-json",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Binary downloads
# --------------------------------------------------------------------------


class _StreamCtx:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aenter__(self) -> "_StreamCtx":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        self.closed = True
        return False

    async def aiter_bytes(self, chunk_size: int | None = None) -> Any:
        for chunk in self.chunks:
            yield chunk


class _StreamingMediaSet:
    """MediaSet double exposing the four download operations."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.ctx = _StreamCtx(chunks)
        self.get_result = lambda *a, **k: self.ctx
        self.read = lambda *a, **k: self.ctx
        self.read_original = lambda *a, **k: self.ctx
        self.retrieve = lambda *a, **k: self.ctx


@pytest.mark.asyncio
async def test_read_download_writes_atomically_and_reports_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    streaming = _StreamingMediaSet([b"media-bytes"])
    media_set = SimpleNamespace(with_streaming_response=streaming)
    root = SimpleNamespace(media_sets=SimpleNamespace(MediaSet=media_set))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)

    class _CfgDownload(_Cfg):
        download_path = str(tmp_path)
        max_download_bytes = 100

    monkeypatch.setattr(cli, "ConfigLoader", _CfgDownload)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "media-set", "read", "ri.ms", "ri.mi", "--output", "item.bin"],
    )
    assert await cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file_size"] == 11
    assert out["truncated"] is False
    assert out["checksum_sha256"]
    assert out["checksum_md5"]
    saved = Path(out["file_path"])
    assert saved.read_bytes() == b"media-bytes"
    assert streaming.ctx.closed


@pytest.mark.asyncio
async def test_read_original_retrieve_get_result_download_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    for operation, arg_suffix in [
        ("read-original", ""),
        ("retrieve", ""),
        ("get-result", " ri.mi job1"),
    ]:
        streaming = _StreamingMediaSet([b"abc"])
        media_set = SimpleNamespace(with_streaming_response=streaming)
        root = SimpleNamespace(media_sets=SimpleNamespace(MediaSet=media_set))
        factory = _Factory(root)
        _patch_main(monkeypatch, factory)

        class _CfgDownload(_Cfg):
            download_path = str(tmp_path)
            max_download_bytes = 100

        monkeypatch.setattr(cli, "ConfigLoader", _CfgDownload)
        argv = (
            ["cmd", "media-set", operation, "ri.ms", "ri.mi", "--output", "out.bin"]
            if not arg_suffix
            else [
                "cmd",
                "media-set",
                operation,
                "ri.ms",
                *arg_suffix.split(),
                "--output",
                "out.bin",
            ]
        )
        monkeypatch.setattr(sys, "argv", argv)
        assert await cli.main() == 0, operation
        out = json.loads(capsys.readouterr().out)
        assert out["file_size"] == 3
        assert out["truncated"] is False
        assert streaming.ctx.closed


@pytest.mark.asyncio
async def test_download_truncates_when_stream_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    streaming = _StreamingMediaSet([b"x" * 200])
    media_set = SimpleNamespace(with_streaming_response=streaming)
    root = SimpleNamespace(media_sets=SimpleNamespace(MediaSet=media_set))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)

    class _CfgDownload(_Cfg):
        download_path = str(tmp_path)
        max_download_bytes = 100

    monkeypatch.setattr(cli, "ConfigLoader", _CfgDownload)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "media-set", "read", "ri.ms", "ri.mi", "--output", "item.bin"],
    )
    assert await cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["truncated"] is True
    assert out["file_size"] == 100
    assert out["source_size_at_least"] == 101


@pytest.mark.asyncio
async def test_download_rejects_unsafe_filename(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    streaming = _StreamingMediaSet([b"abc"])
    media_set = SimpleNamespace(with_streaming_response=streaming)
    root = SimpleNamespace(media_sets=SimpleNamespace(MediaSet=media_set))
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
            ["cmd", "media-set", "read", "ri.ms", "ri.mi", "--output", unsafe],
        )
        assert await cli.main() == 1
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_blocks_nine_write_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    writes = [
        ["media-set", "abort", "ri.ms", "t1"],
        ["media-set", "calculate", "ri.ms", "ri.mi"],
        ["media-set", "clear", "ri.ms", "--media-item-path", "p"],
        ["media-set", "commit", "ri.ms", "t1"],
        ["media-set", "create", "ri.ms"],
        ["media-set", "register", "ri.ms", "--physical-item-name", "n"],
        ["media-set", "transform", "ri.ms", "ri.mi", "--transformation-json", "{}"],
        ["media-set", "upload", "ri.ms", "--file", "a.bin"],
        ["media-set", "upload-media", "--file", "a.bin", "--filename", "a.bin"],
    ]
    for argv in writes:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 8
    assert factory.create_calls == 0


def test_acl_write_classification_matches_design() -> None:
    """AccessControlGuard classifies the 9-op write set as writes."""
    guard = AccessControlGuard(_Cfg(), "MEDIA_SETS")
    for operation in (
        "abort",
        "calculate",
        "clear",
        "commit",
        "create",
        "register",
        "transform",
        "upload",
        "upload_media",
    ):
        assert guard._is_write_operation(operation) is True, operation
    for operation in (
        "get",
        "get_result",
        "get_rid_by_path",
        "get_status",
        "info",
        "metadata",
        "read",
        "read_original",
        "reference",
        "retrieve",
    ):
        assert guard._is_write_operation(operation) is False, operation


# --------------------------------------------------------------------------
# Metadata-only policy
# --------------------------------------------------------------------------


def test_metadata_only_allowlist_parses_exactly() -> None:
    """The packaged allow-list permits exactly 5 of 19 operations."""
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("media_set", "get"),
        ("media_set", "get_rid_by_path"),
        ("media_set", "get_status"),
        ("media_set", "info"),
        ("media_set", "metadata"),
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
    assert len(blocked) == 14


@pytest.mark.asyncio
async def test_metadata_only_permits_five_and_blocks_fourteen(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.ms"})
    calls["get_rid_by_path"].return_value = SimpleNamespace(
        to_dict=lambda: {"media_item_rid": "ri.mi"}
    )
    calls["get_status"].return_value = SimpleNamespace(
        to_dict=lambda: {"status": "RUNNING"}
    )
    calls["info"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.mi"})
    calls["metadata"].return_value = SimpleNamespace(to_dict=lambda: {"type": "image"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    permitted = [
        ["media-set", "get", "ri.ms"],
        ["media-set", "get-rid-by-path", "ri.ms", "--media-item-path", "p"],
        ["media-set", "get-status", "ri.ms", "ri.mi", "job1"],
        ["media-set", "info", "ri.ms", "ri.mi"],
        ["media-set", "metadata", "ri.ms", "ri.mi"],
    ]
    for argv in permitted:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 0, argv
    capsys.readouterr()  # Discard permitted-run output before blocked checks.
    blocked = [
        ["media-set", "abort", "ri.ms", "t1"],
        ["media-set", "calculate", "ri.ms", "ri.mi"],
        ["media-set", "clear", "ri.ms", "--media-item-path", "p"],
        ["media-set", "commit", "ri.ms", "t1"],
        ["media-set", "create", "ri.ms"],
        ["media-set", "get-result", "ri.ms", "ri.mi", "job1", "--output", "o.bin"],
        ["media-set", "read", "ri.ms", "ri.mi", "--output", "o.bin"],
        ["media-set", "read-original", "ri.ms", "ri.mi", "--output", "o.bin"],
        ["media-set", "reference", "ri.ms", "ri.mi"],
        ["media-set", "register", "ri.ms", "--physical-item-name", "n"],
        ["media-set", "retrieve", "ri.ms", "ri.mi", "--output", "o.bin"],
        ["media-set", "transform", "ri.ms", "ri.mi", "--transformation-json", "{}"],
        ["media-set", "upload", "ri.ms", "--file", "a.bin"],
        ["media-set", "upload-media", "--file", "a.bin", "--filename", "a.bin"],
    ]
    for argv in blocked:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8, argv
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == 8, argv


# --------------------------------------------------------------------------
# Attribution (FR-ATTR-4)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_uses_include_attribution_true(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.ms"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "media-set", "get", "ri.ms", "--format", "json"]
    )
    assert await cli.main() == 0
    assert factory.scope_kwargs == {"include_attribution": True}
    assert factory.create_kwargs == {"include_attribution": True}


def test_real_factory_restores_attribution_state_after_success() -> None:
    """invocation_scope restores prior ATTRIBUTION_VAR after success."""
    from foundry_sdk import ATTRIBUTION_VAR

    class _AttrCfg:
        enable_attribution = True
        attribution_rids = "ri.attr.one, ri.attr.two"
        enable_tracing = False
        global_readonly = False
        global_metadata_only = False

    prior = ATTRIBUTION_VAR.get()
    try:
        ATTRIBUTION_VAR.set(["ri.attr.prior"])
        factory = AsyncClientFactory()
        with factory.invocation_scope(_AttrCfg(), include_attribution=True):
            assert ATTRIBUTION_VAR.get() == ["ri.attr.one", "ri.attr.two"]
        assert ATTRIBUTION_VAR.get() == ["ri.attr.prior"]
    finally:
        ATTRIBUTION_VAR.set(prior)


def test_real_factory_restores_attribution_state_after_failure() -> None:
    """invocation_scope restores prior ATTRIBUTION_VAR after an exception."""
    from foundry_sdk import ATTRIBUTION_VAR

    class _AttrCfg:
        enable_attribution = True
        attribution_rids = "ri.attr.one"
        enable_tracing = False
        global_readonly = False
        global_metadata_only = False

    prior = ATTRIBUTION_VAR.get()
    try:
        ATTRIBUTION_VAR.set(["ri.attr.prior"])
        factory = AsyncClientFactory()
        with pytest.raises(RuntimeError):
            with factory.invocation_scope(_AttrCfg(), include_attribution=True):
                raise RuntimeError("boom")
        assert ATTRIBUTION_VAR.get() == ["ri.attr.prior"]
    finally:
        ATTRIBUTION_VAR.set(prior)


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
        sys, "argv", ["cmd", "media-set", "get", "ri.ms", "--timeout", "0"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


@pytest.mark.asyncio
async def test_sdk_error_maps_to_server_error_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].side_effect = RuntimeError("boom")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "media-set", "get", "ri.ms", "--format", "json"]
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
    calls["get"].side_effect = TimeoutError("slow")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "media-set", "get", "ri.ms", "--format", "json"]
    )
    assert await cli.main() == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 5


@pytest.mark.asyncio
async def test_toon_output_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.ms"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "media-set", "get", "ri.ms", "--format", "toon"]
    )
    assert await cli.main() == 0
    assert "rid" in capsys.readouterr().out


def test_console_main_wraps_async_entry() -> None:
    with patch.object(cli, "asyncio") as mock_asyncio:
        mock_asyncio.run.return_value = 0
        assert cli.console_main() == 0
        mock_asyncio.run.assert_called_once()
