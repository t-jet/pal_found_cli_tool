"""Tests for the 23-operation Foundry Models CLI.

Covers the exact catalog, parser surface, nested SDK routing and dispatch,
JSON validation, the four cursor-paged commands, service slicing, the three
streamed downloads, access control write classification and read-only mode,
the packaged 12/11 metadata-only policy, attribution suppression, retry and
error taxonomy, timeouts, output formats, privacy, and the console boundary.

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

from pal_found_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from pal_found_cli.models.scripts import pal_found_models_cli as cli


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
    """Build a nested Models SDK fake rooted at client.models."""
    calls: dict[str, AsyncMock] = {}

    def tracked(name: str) -> AsyncMock:
        mock = AsyncMock()
        calls[name] = mock
        return mock

    live_deployment = SimpleNamespace(transform_json=tracked("transform_json"))
    version = SimpleNamespace(
        create=tracked("version_create"),
        get=tracked("version_get"),
        list=tracked("version_list"),
    )
    series = SimpleNamespace(
        json=tracked("series_json"),
        parquet=tracked("series_parquet"),
    )
    artifact = SimpleNamespace(
        json=tracked("artifact_json"),
        parquet=tracked("artifact_parquet"),
    )
    experiment = SimpleNamespace(
        get=tracked("experiment_get"),
        search=tracked("experiment_search"),
        Series=series,
        ArtifactTable=artifact,
    )
    model = SimpleNamespace(
        create=tracked("model_create"),
        get=tracked("model_get"),
        promote_version=tracked("promote_version"),
        Version=version,
        Experiment=experiment,
    )
    config_version = SimpleNamespace(
        create=tracked("mscv_create"),
        get=tracked("mscv_get"),
        latest=tracked("mscv_latest"),
        list=tracked("mscv_list"),
    )
    run = SimpleNamespace(list=tracked("msr_list"))
    model_studio = SimpleNamespace(
        create=tracked("ms_create"),
        get=tracked("ms_get"),
        launch=tracked("ms_launch"),
        ConfigVersion=config_version,
        Run=run,
    )
    trainer = SimpleNamespace(
        get=tracked("trainer_get"),
        list=tracked("trainer_list"),
    )
    root = SimpleNamespace(
        models=SimpleNamespace(
            LiveDeployment=live_deployment,
            Model=model,
            ModelStudio=model_studio,
            ModelStudioTrainer=trainer,
        )
    )
    return root, calls


def _patch_main(monkeypatch: pytest.MonkeyPatch, factory: _Factory) -> None:
    monkeypatch.setattr(cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_catalog_contains_exact_23_operations() -> None:
    assert len(cli.OP_SPECS) == 23
    # Uniqueness is per (resource, operation) pair, not per method name.
    pairs = [(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS]
    assert len(set(pairs)) == 23
    assert len(cli.OPERATION_BY_RESOURCE) == 10
    seen: list[tuple[str, str, tuple[str, ...], str]] = []
    for spec in cli.OP_SPECS:
        seen.append(
            (spec["resource"], spec["operation"], spec["client_path"], spec["method"])
        )
    assert seen == [
        ("live_deployment", "transform_json", ("LiveDeployment",), "transform_json"),
        ("model", "create", ("Model",), "create"),
        ("model", "get", ("Model",), "get"),
        ("model", "promote_version", ("Model",), "promote_version"),
        ("model_version", "create", ("Model", "Version"), "create"),
        ("model_version", "get", ("Model", "Version"), "get"),
        ("model_version", "list", ("Model", "Version"), "list"),
        ("experiment", "get", ("Model", "Experiment"), "get"),
        ("experiment", "search", ("Model", "Experiment"), "search"),
        ("experiment_series", "json", ("Model", "Experiment", "Series"), "json"),
        ("experiment_series", "parquet", ("Model", "Experiment", "Series"), "parquet"),
        ("experiment_artifact_table", "json", ("Model", "Experiment", "ArtifactTable"), "json"),
        ("experiment_artifact_table", "parquet", ("Model", "Experiment", "ArtifactTable"), "parquet"),
        ("model_studio", "create", ("ModelStudio",), "create"),
        ("model_studio", "get", ("ModelStudio",), "get"),
        ("model_studio", "launch", ("ModelStudio",), "launch"),
        ("model_studio_config_version", "create", ("ModelStudio", "ConfigVersion"), "create"),
        ("model_studio_config_version", "get", ("ModelStudio", "ConfigVersion"), "get"),
        ("model_studio_config_version", "latest", ("ModelStudio", "ConfigVersion"), "latest"),
        ("model_studio_config_version", "list", ("ModelStudio", "ConfigVersion"), "list"),
        ("model_studio_run", "list", ("ModelStudio", "Run"), "list"),
        ("model_studio_trainer", "get", ("ModelStudioTrainer",), "get"),
        ("model_studio_trainer", "list", ("ModelStudioTrainer",), "list"),
    ]


def test_catalog_marks_exactly_four_paged_operations() -> None:
    assert cli.PAGINATED_OPS == frozenset(
        {
            ("experiment", "search"),
            ("model_version", "list"),
            ("model_studio_config_version", "list"),
            ("model_studio_run", "list"),
        }
    )


def test_catalog_marks_exactly_three_download_operations() -> None:
    assert cli.DOWNLOAD_OPS == frozenset(
        {
            ("experiment_series", "parquet"),
            ("experiment_artifact_table", "json"),
            ("experiment_artifact_table", "parquet"),
        }
    )


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


def test_parser_accepts_every_declared_argument() -> None:
    _parse(["live-deployment", "transform-json", "ri.ld", "--input-json", "{}"])
    _parse(["model", "create", "--name", "m", "--parent-folder-rid", "ri.f"])
    _parse(["model", "get", "ri.m"])
    _parse(["model", "promote-version", "ri.m", "--source-model-version-rid", "ri.v"])
    _parse(
        [
            "model-version",
            "create",
            "ri.m",
            "--backing-repositories-json",
            '["ri.r"]',
            "--conda-requirements-json",
            '["pkg"]',
            "--model-api-json",
            "{}",
            "--model-files-json",
            "{}",
        ]
    )
    _parse(["model-version", "get", "ri.m", "ri.v"])
    _parse(["model-version", "list", "ri.m", "--page-size", "5", "--page-token", "t", "--all"])
    _parse(["model-version", "list", "ri.m", "--max-pages", "2"])
    _parse(["experiment", "get", "ri.m", "ri.e"])
    _parse(
        [
            "experiment",
            "search",
            "ri.m",
            "--where-json",
            "{}",
            "--order-by-json",
            "{}",
            "--max-pages",
            "3",
        ]
    )
    _parse(
        [
            "experiment-series",
            "json",
            "ri.m",
            "ri.e",
            "acc",
            "--offset",
            "5",
            "--page-size",
            "3",
        ]
    )
    _parse(
        ["experiment-series", "parquet", "ri.m", "ri.e", "acc", "--output", "m.bin"]
    )
    _parse(
        [
            "experiment-artifact-table",
            "json",
            "ri.m",
            "ri.e",
            "tbl",
            "--output",
            "t.json",
            "--offset",
            "1",
            "--page-size",
            "2",
        ]
    )
    _parse(
        [
            "experiment-artifact-table",
            "parquet",
            "ri.m",
            "ri.e",
            "tbl",
            "--output",
            "t.parquet",
        ]
    )
    _parse(["model-studio", "create", "--name", "s", "--parent-folder-rid", "ri.f"])
    _parse(["model-studio", "get", "ri.ms"])
    _parse(["model-studio", "launch", "ri.ms"])
    _parse(
        [
            "model-studio-config-version",
            "create",
            "ri.ms",
            "--name",
            "v1",
            "--resources-json",
            "{}",
            "--trainer-id",
            "t1",
            "--worker-config-json",
            "{}",
            "--changelog",
            "note",
        ]
    )
    _parse(["model-studio-config-version", "get", "ri.ms", "v1"])
    _parse(["model-studio-config-version", "latest", "ri.ms"])
    _parse(["model-studio-config-version", "list", "ri.ms", "--all"])
    _parse(["model-studio-run", "list", "ri.ms", "--config-version", "v1", "--max-pages", "2"])
    _parse(["model-studio-trainer", "get", "t1", "--version", "1"])
    _parse(["model-studio-trainer", "list"])


def test_unknown_operation_is_rejected() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["model", "list"])
    with pytest.raises(cli.CLIInputError):
        _parse(["bogus", "get"])


def test_trainer_list_exposes_no_pagination() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["model-studio-trainer", "list", "--page-size", "5"])
    with pytest.raises(cli.CLIInputError):
        _parse(["model-studio-trainer", "list", "--max-pages", "2"])


def test_non_paged_commands_reject_pagination_flags() -> None:
    with pytest.raises(cli.CLIInputError):
        _parse(["model", "get", "ri.m", "--page-size", "5"])
    with pytest.raises(cli.CLIInputError):
        _parse(["experiment-series", "json", "ri.m", "ri.e", "a", "--all"])


def test_help_exits_zero_and_names_operations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = cli.build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for resource in (
        "live-deployment",
        "experiment",
        "model",
        "model-studio",
        "model-studio-config-version",
        "model-studio-run",
        "model-studio-trainer",
        "model-version",
    ):
        assert resource in out


# --------------------------------------------------------------------------
# JSON validation
# --------------------------------------------------------------------------


def test_json_arguments_decode_before_dispatch() -> None:
    args = _parse(
        [
            "model-version",
            "create",
            "ri.m",
            "--backing-repositories-json",
            '["ri.r"]',
            "--conda-requirements-json",
            '["pkg==1"]',
            "--model-api-json",
            '{"api":"x"}',
            "--model-files-json",
            '{"files":[]}',
        ]
    )
    spec = cli._spec_for("model_version", "create")
    cli._validate_inputs(spec, args)
    assert args.backing_repositories == ["ri.r"]
    assert args.conda_requirements == ["pkg==1"]
    assert args.model_api == {"api": "x"}


def test_invalid_json_stops_before_client() -> None:
    args = _parse(
        ["live-deployment", "transform-json", "ri.ld", "--input-json", "{"]
    )
    spec = cli._spec_for("live_deployment", "transform_json")
    with pytest.raises(cli.CLIInputError, match="valid JSON"):
        cli._validate_inputs(spec, args)


def test_wrong_json_shape_rejected_without_echo() -> None:
    sentinel = "secret-input"
    args = _parse(
        [
            "live-deployment",
            "transform-json",
            "ri.ld",
            "--input-json",
            json.dumps([sentinel]),
        ]
    )
    spec = cli._spec_for("live_deployment", "transform_json")
    with pytest.raises(cli.CLIInputError) as captured:
        cli._validate_inputs(spec, args)
    assert sentinel not in str(captured.value)
    with pytest.raises(cli.CLIInputError, match="JSON object"):
        cli._validate_inputs(spec, args)


@pytest.mark.asyncio
async def test_invalid_json_returns_exit_one_without_side_effects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = _Factory(_root()[0])
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "live-deployment", "transform-json", "ri.ld", "--input-json", "{"],
    )
    assert await cli.main() == 1
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


# --------------------------------------------------------------------------
# Nested routing and dispatch
# --------------------------------------------------------------------------


def test_get_client_uses_exact_nested_routes() -> None:
    root, _ = _root()
    specs = [
        (("LiveDeployment",), root.models.LiveDeployment),
        (("Model",), root.models.Model),
        (("Model", "Version"), root.models.Model.Version),
        (("Model", "Experiment"), root.models.Model.Experiment),
        (("Model", "Experiment", "Series"), root.models.Model.Experiment.Series),
        (
            ("Model", "Experiment", "ArtifactTable"),
            root.models.Model.Experiment.ArtifactTable,
        ),
        (("ModelStudio",), root.models.ModelStudio),
        (("ModelStudio", "ConfigVersion"), root.models.ModelStudio.ConfigVersion),
        (("ModelStudio", "Run"), root.models.ModelStudio.Run),
        (("ModelStudioTrainer",), root.models.ModelStudioTrainer),
    ]
    for client_path, expected in specs:
        assert cli._get_client(root, client_path) is expected


@pytest.mark.asyncio
async def test_model_get_dispatch_exact_arguments(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["model_get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.m"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "model", "get", "ri.m", "--format", "json"])
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.m"}
    calls["model_get"].assert_awaited_once_with("ri.m", request_timeout=30)
    assert factory.scope_kwargs == {"include_attribution": False}
    assert factory.create_kwargs == {"include_attribution": False}


@pytest.mark.asyncio
async def test_optional_arguments_omitted_when_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["trainer_get"].return_value = SimpleNamespace(to_dict=lambda: {"id": "t1"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "model-studio-trainer", "get", "t1", "--format", "json"]
    )
    assert await cli.main() == 0
    calls["trainer_get"].assert_awaited_once_with("t1", request_timeout=30)


@pytest.mark.asyncio
async def test_transform_json_requires_and_forwards_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["transform_json"].return_value = SimpleNamespace(to_dict=lambda: {"out": 1})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "live-deployment",
            "transform-json",
            "ri.ld",
            "--input-json",
            '{"inputs": [{"feature": 1}]}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["transform_json"].assert_awaited_once_with(
        "ri.ld", input={"inputs": [{"feature": 1}]}, request_timeout=30
    )


@pytest.mark.asyncio
async def test_model_studio_config_version_create_changelog_optional(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["mscv_create"].return_value = SimpleNamespace(to_dict=lambda: {"v": "1"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "model-studio-config-version",
            "create",
            "ri.ms",
            "--name",
            "v1",
            "--resources-json",
            '{"numCores": 4}',
            "--trainer-id",
            "t1",
            "--worker-config-json",
            '{"numWorkers": 1}',
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["mscv_create"].assert_awaited_once_with(
        "ri.ms",
        name="v1",
        resources={"numCores": 4},
        trainer_id="t1",
        worker_config={"numWorkers": 1},
        request_timeout=30,
    )


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def _page_response(items: list[Any], next_token: str | None) -> Any:
    return SimpleNamespace(
        decode=lambda: SimpleNamespace(data=items, next_page_token=next_token)
    )


class _RawList:
    """Cursor-driven raw response double for a paged operation."""

    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        token = kwargs.get("page_token")
        if token is None:
            items, nxt = self.pages[0]
        else:
            for index, (_, nxt) in enumerate(self.pages):
                if nxt == token:
                    items, _ = self.pages[index + 1]
                    nxt = self.pages[index + 1][1]
                    break
            else:
                items, nxt = [], None
        return _page_response(items, nxt)


class _RawVersion:
    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self.list = _RawList(pages)
        self.search = _RawList(pages)


@pytest.mark.asyncio
async def test_paginated_list_uses_raw_response_and_helper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _RawVersion(
        [(["a", "b"], "tok1"), (["c"], None)]
    )
    version = SimpleNamespace(with_raw_response=raw)
    model = SimpleNamespace(Version=version)
    root = SimpleNamespace(models=SimpleNamespace(Model=model))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "model-version", "list", "ri.m", "--max-pages", "5", "--format", "json"],
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == ["a", "b", "c"]
    assert len(raw.list.calls) == 2
    assert raw.list.calls[0]["page_token"] is None
    assert raw.list.calls[1]["page_token"] == "tok1"


@pytest.mark.asyncio
async def test_paginated_defaults_to_single_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _RawVersion([(["a", "b"], "tok1"), (["c"], None)])
    version = SimpleNamespace(with_raw_response=raw)
    model = SimpleNamespace(Version=version)
    root = SimpleNamespace(models=SimpleNamespace(Model=model))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "model-version", "list", "ri.m", "--format", "json"]
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == ["a", "b"]
    assert len(raw.list.calls) == 1


@pytest.mark.asyncio
async def test_series_json_forwards_slicing_once_not_pagination(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["series_json"].return_value = SimpleNamespace(to_dict=lambda: {"pts": [1]})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "experiment-series",
            "json",
            "ri.m",
            "ri.e",
            "acc",
            "--offset",
            "5",
            "--page-size",
            "3",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    calls["series_json"].assert_awaited_once_with(
        "ri.m", "ri.e", "acc", offset=5, page_size=3, request_timeout=30
    )
    # No pagination metadata should be emitted.
    assert "pages_fetched" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# Downloads
# --------------------------------------------------------------------------


class _StreamResp:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def aiter_bytes(self, chunk_size: int | None = None) -> Any:
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


class _StreamingSeries:
    def __init__(self, chunks: list[bytes]) -> None:
        self.ctx = _StreamCtx(chunks)

    def parquet(self, *args: Any, **kwargs: Any) -> _StreamCtx:
        return self.ctx

    def json(self, *args: Any, **kwargs: Any) -> _StreamCtx:
        return self.ctx


@pytest.mark.asyncio
async def test_download_writes_atomically_and_reports_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    streaming = _StreamingSeries([b"abc"])
    series = SimpleNamespace(with_streaming_response=streaming)
    experiment = SimpleNamespace(Series=series)
    model = SimpleNamespace(Experiment=experiment)
    root = SimpleNamespace(models=SimpleNamespace(Model=model))
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
            "experiment-series",
            "parquet",
            "ri.m",
            "ri.e",
            "acc",
            "--output",
            "m.bin",
        ],
    )
    assert await cli.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["file_size"] == 3
    assert out["truncated"] is False
    saved = Path(out["file_path"])
    assert saved.read_bytes() == b"abc"
    assert streaming.ctx.closed


@pytest.mark.asyncio
async def test_download_rejects_unsafe_filename(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    streaming = _StreamingSeries([b"abc"])
    series = SimpleNamespace(with_streaming_response=streaming)
    experiment = SimpleNamespace(Series=series)
    model = SimpleNamespace(Experiment=experiment)
    root = SimpleNamespace(models=SimpleNamespace(Model=model))
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
            [
                "cmd",
                "experiment-series",
                "parquet",
                "ri.m",
                "ri.e",
                "acc",
                "--output",
                unsafe,
            ],
        )
        assert await cli.main() == 1
        out = capsys.readouterr().out
        envelope = json.loads(out)
        assert envelope["exit_code"] == 1


# --------------------------------------------------------------------------
# Access control
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_readonly_blocks_write_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    writes = [
        ["live-deployment", "transform-json", "ri.ld", "--input-json", "{}"],
        ["model", "create", "--name", "m", "--parent-folder-rid", "ri.f"],
        ["model", "promote-version", "ri.m", "--source-model-version-rid", "ri.v"],
        ["model-version", "create", "ri.m", "--backing-repositories-json", "[]", "--conda-requirements-json", "[]", "--model-api-json", "{}", "--model-files-json", "{}"],
        ["model-studio", "create", "--name", "s", "--parent-folder-rid", "ri.f"],
        ["model-studio", "launch", "ri.ms"],
        ["model-studio-config-version", "create", "ri.ms", "--name", "v", "--resources-json", "{}", "--trainer-id", "t", "--worker-config-json", "{}"],
    ]
    for argv in writes:
        monkeypatch.setattr(sys, "argv", ["cmd", *argv])
        assert await cli.main() == 8
        out = capsys.readouterr().out
        envelope = json.loads(out)
        assert envelope["exit_code"] == 8
    # Read remains permitted.
    calls = _root()[1]
    root2, _ = _root()
    calls2 = root2.models.Model.get
    calls2.return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.m"})
    factory2 = _Factory(root2)
    _patch_main(monkeypatch, factory2)
    monkeypatch.setattr(sys, "argv", ["cmd", "model", "get", "ri.m", "--format", "json"])
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"rid": "ri.m"}


@pytest.mark.asyncio
async def test_experiment_search_is_semantic_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = _RawVersion([([{"id": "e1"}], None)])
    experiment = SimpleNamespace(with_raw_response=raw)
    model = SimpleNamespace(Experiment=experiment)
    root = SimpleNamespace(models=SimpleNamespace(Model=model))
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cmd",
            "experiment",
            "search",
            "ri.m",
            "--max-pages",
            "1",
            "--format",
            "json",
        ],
    )
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == [{"id": "e1"}]


@pytest.mark.asyncio
async def test_acl_denial_reports_rule_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "model-studio", "launch", "ri.ms"],
    )
    assert await cli.main() == 8
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["exit_code"] == 8
    # The denying rule is surfaced in the structured envelope message.
    assert "metadata-only mode active" in envelope["message"]


def test_metadata_only_permits_exactly_12() -> None:
    permitted = {
        ("experiment", "get"),
        ("experiment", "search"),
        ("model", "get"),
        ("model_studio", "get"),
        ("model_studio_config_version", "get"),
        ("model_studio_config_version", "latest"),
        ("model_studio_config_version", "list"),
        ("model_studio_run", "list"),
        ("model_studio_trainer", "get"),
        ("model_studio_trainer", "list"),
        ("model_version", "get"),
        ("model_version", "list"),
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


def test_metadata_only_blocks_remaining_11() -> None:
    catalog = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    permitted = {
        ("experiment", "get"),
        ("experiment", "search"),
        ("model", "get"),
        ("model_studio", "get"),
        ("model_studio_config_version", "get"),
        ("model_studio_config_version", "latest"),
        ("model_studio_config_version", "list"),
        ("model_studio_run", "list"),
        ("model_studio_trainer", "get"),
        ("model_studio_trainer", "list"),
        ("model_version", "get"),
        ("model_version", "list"),
    }
    assert catalog - permitted == {
        ("live_deployment", "transform_json"),
        ("model", "create"),
        ("model", "promote_version"),
        ("model_studio", "create"),
        ("model_studio", "launch"),
        ("model_studio_config_version", "create"),
        ("model_version", "create"),
        ("experiment_series", "json"),
        ("experiment_series", "parquet"),
        ("experiment_artifact_table", "json"),
        ("experiment_artifact_table", "parquet"),
    }


# --------------------------------------------------------------------------
# Timeouts, error taxonomy, output, privacy
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
    factory = _Factory(_root()[0])
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys, "argv", ["cmd", "model", "get", "ri.m", "--timeout", "9999"]
    )
    assert await cli.main() == 1
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["exit_code"] == 1
    assert factory.create_calls == 0


@pytest.mark.asyncio
async def test_error_maps_to_expected_exit_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry_sdk._errors import (
        BadRequestError,
        NotFoundError,
        PermissionDeniedError,
        ServiceUnavailable,
        UnauthorizedError,
    )

    cases = [
        (BadRequestError({"errorName": "BadRequest", "errorInstanceId": "x"}), 1),
        (UnauthorizedError({"errorName": "Unauthorized", "errorInstanceId": "x"}), 2),
        (PermissionDeniedError({"errorName": "PermissionDenied", "errorInstanceId": "x"}), 3),
        (NotFoundError({"errorName": "NotFound", "errorInstanceId": "x"}), 4),
        (TimeoutError("slow"), 5),
        (ServiceUnavailable("503", "THROTTLED"), 6),
    ]
    for error, expected_code in cases:
        root, calls = _root()
        calls["model_get"].side_effect = error
        factory = _Factory(root)
        _patch_main(monkeypatch, factory)
        monkeypatch.setattr(sys, "argv", ["cmd", "model", "get", "ri.m"])
        assert await cli.main() == expected_code
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["exit_code"] == expected_code


@pytest.mark.asyncio
async def test_no_secrets_in_errors_or_tracebacks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sentinel = "sentinel-token-secret"
    root, calls = _root()
    calls["model_get"].side_effect = RuntimeError(sentinel)
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "model", "get", "ri.m"])
    assert await cli.main() == 6
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


@pytest.mark.asyncio
async def test_output_formats_and_attribution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, calls = _root()
    calls["model_get"].return_value = SimpleNamespace(to_dict=lambda: {"rid": "ri.m"})
    factory = _Factory(root)
    _patch_main(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cmd", "model", "get", "ri.m", "--format", "json", "--pretty"],
    )
    assert await cli.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"rid": "ri.m"}
    assert factory.scope_kwargs == {"include_attribution": False}
    assert factory.create_kwargs == {"include_attribution": False}


def test_console_main_uses_one_asyncio_run_boundary(
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
