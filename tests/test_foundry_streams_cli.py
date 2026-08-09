"""Tests for the 15-operation Foundry Streams CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch,
JSON validation, the ADR-003 batch-read contract (--max-records bounds and
mapping to the SDK limit), binary publish file handling, access control
write classification including the reset-verb regression (stream.reset and
subscriber.reset_offsets must stay write-classified), read-only mode, the
packaged 3/12 metadata-only policy, attribution suppression, retry and error
taxonomy, timeouts (including the streams namespace default), output
formats, privacy, and the console boundary.

All SDK transport is mocked: no live Foundry connection is ever made.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foundry_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from foundry_cli.streams.scripts import foundry_streams_cli as cli


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
    """Build a nested Streams SDK fake rooted at client.streams."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    subscriber = SimpleNamespace(
        create=tracked("subscriber_create"),
        commit_offsets=tracked("subscriber_commit_offsets"),
        delete=tracked("subscriber_delete"),
        get_read_position=tracked("subscriber_get_read_position"),
        read_records=tracked("subscriber_read_records"),
        reset_offsets=tracked("subscriber_reset_offsets"),
    )
    stream = SimpleNamespace(
        create=tracked("stream_create"),
        get=tracked("stream_get"),
        get_end_offsets=tracked("stream_get_end_offsets"),
        get_records=tracked("stream_get_records"),
        publish_binary_record=tracked("stream_publish_binary_record"),
        publish_record=tracked("stream_publish_record"),
        publish_records=tracked("stream_publish_records"),
        reset=tracked("stream_reset"),
        Subscriber=subscriber,
    )
    dataset = SimpleNamespace(create=tracked("dataset_create"), Stream=stream)
    root = SimpleNamespace(streams=SimpleNamespace(Dataset=dataset))
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_15_operations() -> None:
    assert len(cli.OP_SPECS) == 15
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 15
    assert len(cli.OPERATION_BY_RESOURCE) == 3
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("dataset", "create", ("Dataset",), "create"),
        ("stream", "create", ("Dataset", "Stream"), "create"),
        ("stream", "get", ("Dataset", "Stream"), "get"),
        ("stream", "get_end_offsets", ("Dataset", "Stream"), "get_end_offsets"),
        ("stream", "get_records", ("Dataset", "Stream"), "get_records"),
        (
            "stream",
            "publish_binary_record",
            ("Dataset", "Stream"),
            "publish_binary_record",
        ),
        ("stream", "publish_record", ("Dataset", "Stream"), "publish_record"),
        ("stream", "publish_records", ("Dataset", "Stream"), "publish_records"),
        ("stream", "reset", ("Dataset", "Stream"), "reset"),
        ("subscriber", "create", ("Dataset", "Stream", "Subscriber"), "create"),
        (
            "subscriber",
            "commit_offsets",
            ("Dataset", "Stream", "Subscriber"),
            "commit_offsets",
        ),
        ("subscriber", "delete", ("Dataset", "Stream", "Subscriber"), "delete"),
        (
            "subscriber",
            "get_read_position",
            ("Dataset", "Stream", "Subscriber"),
            "get_read_position",
        ),
        (
            "subscriber",
            "read_records",
            ("Dataset", "Stream", "Subscriber"),
            "read_records",
        ),
        (
            "subscriber",
            "reset_offsets",
            ("Dataset", "Stream", "Subscriber"),
            "reset_offsets",
        ),
    ]


def test_catalog_marks_exactly_two_batch_read_operations() -> None:
    assert cli.BATCH_READ_OPS == {
        ("stream", "get_records"): 10_000,
        ("subscriber", "read_records"): 1_000,
    }


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(
        [
            "dataset",
            "create",
            "--name",
            "d",
            "--parent-folder-rid",
            "ri.f",
            "--schema-json",
            '{"key_field_names":[],"fields":[],"change_data_capture":false}',
        ]
    )
    _parse(["stream", "get", "ri.d", "master"])
    _parse(["stream", "get-records", "ri.d", "master", "--partition-id", "0", "--max-records", "10"])
    _parse(
        [
            "stream",
            "publish-binary-record",
            "ri.d",
            "master",
            "--file",
            "payload.bin",
        ]
    )
    _parse(["stream", "publish-record", "ri.d", "master", "--record-json", "{}"])
    _parse(["stream", "publish-records", "ri.d", "master", "--records-json", "[]"])
    _parse(["stream", "reset", "ri.d", "master"])
    _parse(
        [
            "subscriber",
            "create",
            "ri.d",
            "master",
            "--subscriber-id",
            "s1",
            "--read-position-json",
            "{}",
        ]
    )
    _parse(
        [
            "subscriber",
            "commit-offsets",
            "ri.d",
            "master",
            "s1",
            "--offsets-json",
            '{"0":5}',
        ]
    )
    _parse(["subscriber", "delete", "ri.d", "master", "s1"])
    _parse(["subscriber", "get-read-position", "ri.d", "master", "s1"])
    _parse(
        [
            "subscriber",
            "read-records",
            "ri.d",
            "master",
            "s1",
            "--auto-commit",
            "--partition-ids-json",
            '["0"]',
            "--max-records",
            "5",
        ]
    )
    _parse(
        ["subscriber", "reset-offsets", "ri.d", "master", "s1", "--position-json", "{}"]
    )
    _parse(
        ["stream", "get", "ri.d", "master", "--timeout", "120", "--format", "toon"]
    )


def test_parser_rejects_unknown_operation() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["stream", "nope"])


def test_parser_rejects_max_records_above_bound() -> None:
    spec_get = cli.OPERATION_BY_RESOURCE["stream"]["get_records"]
    args_get = _parse(
        ["stream", "get-records", "ri.d", "master", "--partition-id", "0", "--max-records", "10001"]
    )
    with pytest.raises(cli.CLIInputError):
        cli._read_max_records(args_get, spec_get)
    spec_read = cli.OPERATION_BY_RESOURCE["subscriber"]["read_records"]
    args_read = _parse(
        ["subscriber", "read-records", "ri.d", "master", "s1", "--max-records", "1001"]
    )
    with pytest.raises(cli.CLIInputError):
        cli._read_max_records(args_read, spec_read)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dataset_create_dispatches_with_json_schema(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["dataset_create"].return_value = SimpleNamespace(
        to_dict=lambda: {"rid": "ri.d"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "dataset",
            "create",
            "--name",
            "d",
            "--parent-folder-rid",
            "ri.f",
            "--schema-json",
            '{"key_field_names":[],"fields":[],"change_data_capture":false}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.d"}
    calls["dataset_create"].assert_awaited_once_with(
        name="d",
        parent_folder_rid="ri.f",
        schema={"key_field_names": [], "fields": [], "change_data_capture": False},
        request_timeout=120,
    )


@pytest.mark.asyncio
async def test_stream_get_dispatches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"branch_name": "master"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "stream", "get", "ri.d", "master", "--format", "json"]
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"branch_name": "master"}
    calls["stream_get"].assert_awaited_once_with("ri.d", "master", request_timeout=120)


@pytest.mark.asyncio
async def test_get_records_batch_read_maps_max_records_to_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_get_records"].return_value = [{"offset": 0, "value": "v1"}]
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "stream",
            "get-records",
            "ri.d",
            "master",
            "--partition-id",
            "0",
            "--max-records",
            "7",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == [{"offset": 0, "value": "v1"}]
    calls["stream_get_records"].assert_awaited_once_with(
        "ri.d", "master", partition_id="0", limit=7, request_timeout=120
    )


@pytest.mark.asyncio
async def test_subscriber_read_records_aggregates_batch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["subscriber_read_records"].return_value = SimpleNamespace(
        to_dict=lambda: {"records_by_partition": {"0": [{"offset": 1}]}}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "subscriber",
            "read-records",
            "ri.d",
            "master",
            "s1",
            "--auto-commit",
            "--partition-ids-json",
            '["0"]',
            "--max-records",
            "5",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "records_by_partition": {"0": [{"offset": 1}]}
    }
    calls["subscriber_read_records"].assert_awaited_once_with(
        "ri.d",
        "master",
        "s1",
        auto_commit=True,
        partition_ids=["0"],
        limit=5,
        request_timeout=120,
    )


@pytest.mark.asyncio
async def test_publish_record_with_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_publish_record"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "stream",
            "publish-record",
            "ri.d",
            "master",
            "--record-json",
            '{"data":"x"}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["stream_publish_record"].assert_awaited_once_with(
        "ri.d", "master", record={"data": "x"}, request_timeout=120
    )


@pytest.mark.asyncio
async def test_publish_binary_record_reads_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root, calls = _root()
    calls["stream_publish_binary_record"].return_value = None
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"binary-content")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "stream",
            "publish-binary-record",
            "ri.d",
            "master",
            "--file",
            str(payload),
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["stream_publish_binary_record"].assert_awaited_once_with(
        "ri.d", "master", b"binary-content", request_timeout=120
    )


@pytest.mark.asyncio
async def test_publish_binary_record_missing_file_rejected_before_client(
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
            "stream",
            "publish-binary-record",
            "ri.d",
            "master",
            "--file",
            "missing.bin",
        ],
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_subscriber_commit_offsets_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["subscriber_commit_offsets"].return_value = SimpleNamespace(
        to_dict=lambda: {"0": 5}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "subscriber",
            "commit-offsets",
            "ri.d",
            "master",
            "s1",
            "--offsets-json",
            '{"0":5}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"0": 5}
    calls["subscriber_commit_offsets"].assert_awaited_once_with(
        "ri.d", "master", "s1", offsets={"0": 5}, request_timeout=120
    )


@pytest.mark.asyncio
async def test_subscriber_reset_offsets_position_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["subscriber_reset_offsets"].return_value = SimpleNamespace(
        to_dict=lambda: {"0": 0}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "subscriber",
            "reset-offsets",
            "ri.d",
            "master",
            "s1",
            "--position-json",
            '{"0":0}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"0": 0}
    calls["subscriber_reset_offsets"].assert_awaited_once_with(
        "ri.d", "master", "s1", position={"0": 0}, request_timeout=120
    )


@pytest.mark.asyncio
async def test_stream_reset_dispatches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_reset"].return_value = SimpleNamespace(
        to_dict=lambda: {"branch_name": "master"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "stream", "reset", "ri.d", "master", "--format", "json"],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"branch_name": "master"}
    calls["stream_reset"].assert_awaited_once_with("ri.d", "master", request_timeout=120)


@pytest.mark.asyncio
async def test_unknown_operation_returns_user_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "stream", "nope"])
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_blocks_ten_write_operations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    writes = [
        ["dataset", "create", "--name", "d", "--parent-folder-rid", "ri.f", "--schema-json", "{}"],
        ["stream", "create", "ri.d", "--branch-name", "b", "--schema-json", "{}"],
        ["stream", "publish-binary-record", "ri.d", "master", "--file", "x.bin"],
        ["stream", "publish-record", "ri.d", "master", "--record-json", "{}"],
        ["stream", "publish-records", "ri.d", "master", "--records-json", "[]"],
        ["stream", "reset", "ri.d", "master"],
        ["subscriber", "create", "ri.d", "master", "--subscriber-id", "s"],
        ["subscriber", "commit-offsets", "ri.d", "master", "s", "--offsets-json", "{}"],
        ["subscriber", "delete", "ri.d", "master", "s"],
        ["subscriber", "reset-offsets", "ri.d", "master", "s", "--position-json", "{}"],
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
    calls["stream_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"branch_name": "master"}
    )
    calls["stream_get_records"].return_value = []
    calls["subscriber_get_read_position"].return_value = SimpleNamespace(
        to_dict=lambda: {"0": 0}
    )
    calls["subscriber_read_records"].return_value = SimpleNamespace(
        to_dict=lambda: {"records_by_partition": {}}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)

    monkeypatch.setattr(sys, "argv", ["cmd", "stream", "get", "ri.d", "master", "--format", "json"])
    assert await cli.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "stream",
            "get-records",
            "ri.d",
            "master",
            "--partition-id",
            "0",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "subscriber", "get-read-position", "ri.d", "master", "s1", "--format", "json"],
    )
    assert await cli.main() == 0
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "subscriber", "read-records", "ri.d", "master", "s1", "--format", "json"],
    )
    assert await cli.main() == 0


def test_reset_verbs_stay_write_classified_under_narrow_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stream.reset and subscriber.reset_offsets must stay write-classified.

    Narrower override variables (operation-level ENABLED, READONLY=false, or
    METADATA_ONLY=false) must not reclassify these verbs as reads. The write
    classification comes from _WRITE_VERBS; under global READONLY=true the
    write block applies, and under global METADATA_ONLY=true the write tier
    blocks them outright (allow-list entry would be irrelevant for writes).
    """
    guard = AccessControlGuard(_Cfg(), "STREAMS")
    assert guard._is_write_operation("reset") is True
    assert guard._is_write_operation("reset_offsets") is True
    # A narrower operation-level override must not downgrade classification:
    # under READONLY=true the write verbs remain blocked even when a narrower
    # ENABLED=true override is present.
    monkeypatch.setenv(
        "FOUNDRY_AGENTIC_CLI_STREAMS_STREAM_RESET_ENABLED", "true"
    )
    monkeypatch.setenv(
        "FOUNDRY_AGENTIC_CLI_STREAMS_SUBSCRIBER_RESET_OFFSETS_ENABLED", "true"
    )
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    with pytest.raises(AccessControlError):
        guard.check("stream", "reset")
    with pytest.raises(AccessControlError):
        guard.check("subscriber", "reset_offsets")
    # Under global METADATA_ONLY=true the write tier blocks them regardless of
    # any narrower allow-list-style override.
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "false")
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    with pytest.raises(AccessControlError):
        guard.check("stream", "reset")
    with pytest.raises(AccessControlError):
        guard.check("subscriber", "reset_offsets")


@pytest.mark.asyncio
async def test_readonly_blocks_stream_reset_before_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    monkeypatch.setattr(sys, "argv", ["cmd", "stream", "reset", "ri.d", "master"])
    assert await cli.main() == 8
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 8
    assert factory.create_calls == 0


def test_metadata_only_permits_exactly_3_blocks_12() -> None:
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("stream", "get"),
        ("stream", "get_end_offsets"),
        ("subscriber", "get_read_position"),
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
        ("dataset", "create"),
        ("stream", "create"),
        ("stream", "get_records"),
        ("stream", "publish_binary_record"),
        ("stream", "publish_record"),
        ("stream", "publish_records"),
        ("stream", "reset"),
        ("subscriber", "create"),
        ("subscriber", "commit_offsets"),
        ("subscriber", "delete"),
        ("subscriber", "read_records"),
        ("subscriber", "reset_offsets"),
    }


def test_metadata_only_runtime_blocks_blocked_ops_and_permits_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    guard = AccessControlGuard(_Cfg(), "STREAMS")
    for resource, operation in [
        ("stream", "get_records"),
        ("stream", "reset"),
        ("subscriber", "read_records"),
        ("subscriber", "reset_offsets"),
        ("dataset", "create"),
    ]:
        with pytest.raises(AccessControlError):
            guard.check(resource, operation)
    guard.check("stream", "get")
    guard.check("stream", "get_end_offsets")
    guard.check("subscriber", "get_read_position")


# --------------------------------------------------------------------------
# Attribution suppression
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invocation_uses_include_attribution_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"branch_name": "master"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "stream", "get", "ri.d", "master", "--format", "json"]
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
async def test_streams_default_timeout_used_when_env_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"branch_name": "master"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "stream", "get", "ri.d", "master", "--format", "json"]
    )
    assert await cli.main() == 0
    calls["stream_get"].assert_awaited_once_with("ri.d", "master", request_timeout=120)


@pytest.mark.asyncio
async def test_streams_timeout_env_overrides_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"branch_name": "master"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_STREAMS_TIMEOUT_S", "45")

    class _CfgTimeout(_Cfg):
        def get_int(self, name: str, default: int | None = None) -> int | None:
            return 45

    monkeypatch.setattr(cli, "ConfigLoader", _CfgTimeout)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "stream", "get", "ri.d", "master", "--format", "json"]
    )
    assert await cli.main() == 0
    calls["stream_get"].assert_awaited_once_with("ri.d", "master", request_timeout=45)


@pytest.mark.asyncio
async def test_invalid_timeout_stops_before_acl_or_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "stream", "get", "ri.d", "master", "--timeout", "0"]
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
    calls["stream_get"].side_effect = TimeoutError("timed out")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "stream", "get", "ri.d", "master"])
    assert await cli.main() == 5
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 5


@pytest.mark.asyncio
async def test_output_toon_and_json_formats(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["stream_get"].return_value = SimpleNamespace(
        to_dict=lambda: {"branch_name": "master"}
    )
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "stream", "get", "ri.d", "master", "--format", "toon"]
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert "branch_name" in out


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
    calls["stream_get"].side_effect = Exception("secret-token leaked")
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "stream", "get", "ri.d", "master"])
    assert await cli.main() == 6
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["message"] == "Streams operation failed"
    assert "secret-token" not in envelope["message"]
