#!/usr/bin/env python3
"""Unit tests for the two-operation Foundry Audit CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AbstractContextManager
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.audit.scripts import foundry_audit_cli
from foundry_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from foundry_cli.common.binary_download_handler import InvalidDownloadError
from foundry_cli.common.config_loader import ConfigLoader, ConfigurationError
from foundry_cli.common.error_serializer import (
    EXIT_ACCESS_CONTROL,
    EXIT_AUTH,
    EXIT_CONFIGURATION,
    EXIT_NOT_FOUND,
    EXIT_PERMISSION_DENIED,
    EXIT_RATE_LIMIT,
    EXIT_SERVER_ERROR,
    EXIT_TIMEOUT,
    EXIT_USER_INPUT,
)
from foundry_cli.common.retry import RetryHandler
from foundry_cli.common.tracing_provider import TracingProvider


class _Cfg(ConfigLoader):
    """Small configuration double with every Audit-consumed property."""

    timeout_s = 30
    log_level = "INFO"
    enable_tracing = False
    global_readonly = False
    global_metadata_only = False
    download_path = Path(".foundry-data/downloads")
    max_download_bytes = 1_572_864

    def load(self) -> None:
        """Represent a successful configuration load."""


class _RawResponse:
    def __init__(self, value: Any) -> None:
        self.value = value
        self.decode_calls = 0

    def decode(self) -> Any:
        self.decode_calls += 1
        return self.value


class _Page:
    def __init__(self, data: list[Any], next_page_token: str | None = None) -> None:
        self.data = data
        self.next_page_token = next_page_token


class _RawListEndpoint:
    def __init__(self, pages: dict[str | None, _Page]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list(self, organization_rid: str, **kwargs: Any) -> _RawResponse:
        self.calls.append((organization_rid, kwargs))
        return _RawResponse(self.pages[kwargs.get("page_token")])


class _LogFileClient:
    def __init__(
        self,
        *,
        raw: Any | None = None,
        streaming: Any | None = None,
    ) -> None:
        self.with_raw_response = raw or MagicMock()
        self.with_streaming_response = streaming or MagicMock()


class _Scope(AbstractContextManager[None]):
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    def __enter__(self) -> None:
        if self.events is not None:
            self.events.append("scope-enter")
        return None

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> Literal[False]:
        if self.events is not None:
            self.events.append("scope-exit")
        return False


class _Factory:
    def __init__(self, client: _LogFileClient, events: list[str] | None = None) -> None:
        self.sdk = SimpleNamespace(
            audit=SimpleNamespace(
                Organization=SimpleNamespace(LogFile=client),
            )
        )
        self.events = events
        self.create_calls = 0

    def invocation_scope(self, cfg: Any) -> _Scope:
        return _Scope(self.events)

    def create(self, cfg: Any) -> Any:
        self.create_calls += 1
        if self.events is not None:
            self.events.append("client-create")
        return self.sdk


class _ImmediateRetry:
    async def execute(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await function(*args, **kwargs)


class _HttpError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = SimpleNamespace(status_code=status_code)


def _list_args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "organization_rid": "ri.organization.test",
        "start_date": date(2026, 8, 1),
        "end_date": None,
        "page_size": None,
        "page_token": None,
        "batch_pages": None,
        "timeout": None,
        "format": "json",
        "pretty": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _content_args(**overrides: Any) -> argparse.Namespace:
    values: dict[str, Any] = {
        "organization_rid": "ri.organization.test",
        "log_file_id": "log-file-id",
        "output_filename": "audit.bin",
        "timeout": None,
        "format": "json",
        "pretty": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _patch_main_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    factory: Any,
    *,
    retry: Any | None = None,
    cfg_type: type[_Cfg] = _Cfg,
    guard: Any | None = None,
) -> Any:
    selected_guard = guard or MagicMock()
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", cfg_type)
    monkeypatch.setattr(foundry_audit_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_audit_cli,
        "AccessControlGuard",
        lambda cfg, namespace, **kwargs: selected_guard,
    )
    monkeypatch.setattr(foundry_audit_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(
        foundry_audit_cli,
        "RetryHandler",
        lambda **kwargs: retry or _ImmediateRetry(),
    )
    return selected_guard


def test_catalog_contains_exact_two_unique_nested_operations() -> None:
    assert len(foundry_audit_cli.OP_SPECS) == 2
    assert {
        (
            spec["resource"],
            spec["operation"],
            spec["client_path"],
            spec["method"],
        )
        for spec in foundry_audit_cli.OP_SPECS
    } == {
        ("log_file", "list", "Organization.LogFile", "list"),
        ("log_file", "content", "Organization.LogFile", "content"),
    }
    assert set(foundry_audit_cli.OPERATION_BY_RESOURCE) == {"log_file"}


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            [
                "log-file",
                "list",
                "organization",
                "--start-date",
                "2026-08-01",
                "--end-date",
                "2026-08-02",
                "--page-size",
                "25",
                "--page-token",
                "cursor",
                "--batch-pages",
                "3",
                "--timeout",
                "9",
                "--format",
                "toon",
                "--pretty",
            ],
            ("log-file", "list"),
        ),
        (
            [
                "log-file",
                "content",
                "organization",
                "file-id",
                "--output-filename",
                "audit.bin",
                "--timeout",
                "8",
                "--format",
                "auto",
                "--pretty",
            ],
            ("log-file", "content"),
        ),
    ],
)
def test_parser_accepts_every_declared_argument(
    argv: list[str], expected: tuple[str, str]
) -> None:
    args = foundry_audit_cli.build_parser().parse_args(argv)
    assert (args.resource, args.operation) == expected
    assert args.pretty is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["log-file", "list", "--help"],
        ["log-file", "content", "--help"],
    ],
)
def test_help_exits_zero_and_names_operations(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        foundry_audit_cli.build_parser().parse_args(argv)
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "usage:" in output
    if argv == ["--help"]:
        assert "log-file list" in output
        assert "log-file content" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [["prog"], ["prog", "log-file"]])
async def test_missing_command_returns_one_without_loading_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    loader = MagicMock(side_effect=AssertionError("configuration loaded"))
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", loader)
    monkeypatch.setattr(sys, "argv", argv)
    assert await foundry_audit_cli.main() == EXIT_USER_INPUT
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_USER_INPUT
    loader.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "argv",
    [
        ["prog", "unknown"],
        ["prog", "log-file", "content", "organization"],
        [
            "prog",
            "log-file",
            "list",
            "organization",
            "--start-date",
            "2026-08-01",
            "--page-size",
            "not-an-int",
        ],
    ],
)
async def test_argparse_failures_are_json_user_input_errors_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    loader = MagicMock(side_effect=AssertionError("configuration loaded"))
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", loader)
    monkeypatch.setattr(sys, "argv", argv)
    assert await foundry_audit_cli.main() == EXIT_USER_INPUT
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["error"] is True
    assert envelope["exit_code"] == EXIT_USER_INPUT
    assert "usage:" not in captured.err
    loader.assert_not_called()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("2024-02-29", date(2024, 2, 29))],
)
def test_parse_iso_date_accepts_none_and_real_dates(
    value: str | None, expected: date | None
) -> None:
    assert foundry_audit_cli._parse_iso_date(value, field="start_date") == expected


@pytest.mark.parametrize(
    "value",
    ["20260801", "2026-8-01", "2026-02-30", " 2026-08-01", "2026-08-01 "],
)
def test_parse_iso_date_rejects_non_strict_or_impossible_dates(value: str) -> None:
    with pytest.raises(ValueError, match="start_date"):
        foundry_audit_cli._parse_iso_date(value, field="start_date")


@pytest.mark.parametrize("token", [None, ""])
def test_initial_list_requires_start_date(token: str | None) -> None:
    with pytest.raises(ValueError, match="start_date is required"):
        foundry_audit_cli._validate_list_cursor(None, token)


def test_continuation_token_allows_missing_start_date() -> None:
    foundry_audit_cli._validate_list_cursor(None, "cursor")


@pytest.mark.parametrize("value", [1, 30, 3600])
def test_timeout_accepts_adr_002_bounds(value: int) -> None:
    assert foundry_audit_cli._validate_timeout(value) == value


@pytest.mark.parametrize("value", [0, 3601, -1, True])
def test_timeout_rejects_values_outside_adr_002_bounds(value: Any) -> None:
    with pytest.raises(ValueError, match="between 1 and 3600"):
        foundry_audit_cli._validate_timeout(value)


def test_get_client_uses_exact_audit_organization_log_file_route() -> None:
    log_file = object()
    sdk = SimpleNamespace(
        audit=SimpleNamespace(Organization=SimpleNamespace(LogFile=log_file))
    )
    factory = MagicMock()
    factory.create.return_value = sdk
    assert foundry_audit_cli._get_client(MagicMock(), "log_file", factory) is log_file
    factory.create.assert_called_once()


def test_unknown_operation_is_user_input_error() -> None:
    with pytest.raises(ValueError, match="Unknown operation"):
        foundry_audit_cli._spec_for("log_file", "delete")


def test_model_conversion_supports_sdk_and_nested_models() -> None:
    model = MagicMock()
    model.to_dict.return_value = {"id": "one", "nested": [SimpleNamespace()]}
    converted = foundry_audit_cli._model_to_dict([model])
    assert converted[0]["id"] == "one"
    assert isinstance(converted[0]["nested"], list)


@pytest.mark.asyncio
async def test_fetch_list_page_uses_raw_wrapper_and_decodes_sdk_page() -> None:
    endpoint = _RawListEndpoint({"cursor": _Page([{"id": "two"}], "next")})
    client = _LogFileClient(raw=endpoint)
    result = await foundry_audit_cli._fetch_list_page(
        client,
        organization_rid="organization",
        start_date=None,
        end_date=date(2026, 8, 2),
        page_size=11,
        page_token="cursor",
        request_timeout=17,
    )
    assert result == {"items": [{"id": "two"}], "next_page_token": "next"}
    assert endpoint.calls == [
        (
            "organization",
            {
                "start_date": None,
                "end_date": date(2026, 8, 2),
                "page_size": 11,
                "page_token": "cursor",
                "request_timeout": 17,
            },
        )
    ]


@pytest.mark.asyncio
async def test_list_default_fetches_one_raw_server_page_and_keeps_cursor() -> None:
    endpoint = _RawListEndpoint(
        {None: _Page([{"id": "one"}, {"id": "two"}], "next")}
    )
    records, helper = await foundry_audit_cli._list_log_files(
        _LogFileClient(raw=endpoint), _list_args(page_size=2), 13
    )
    assert records == [{"id": "one"}, {"id": "two"}]
    assert helper.pages_fetched == 1
    assert helper.total_items == 2
    assert helper.next_page_token == "next"


@pytest.mark.asyncio
async def test_list_stops_at_eof_and_forwards_each_cursor() -> None:
    endpoint = _RawListEndpoint(
        {
            None: _Page([{"id": "one"}], "second"),
            "second": _Page([{"id": "two"}], None),
        }
    )
    records, helper = await foundry_audit_cli._list_log_files(
        _LogFileClient(raw=endpoint), _list_args(page_size=1, batch_pages=8), 4
    )
    assert records == [{"id": "one"}, {"id": "two"}]
    assert helper.pages_fetched == 2
    assert [call[1]["page_token"] for call in endpoint.calls] == [None, "second"]


@pytest.mark.asyncio
async def test_list_hard_caps_batch_at_40_actual_pages() -> None:
    pages = {
        None if index == 0 else f"p{index}": _Page(
            [{"id": index}], f"p{index + 1}"
        )
        for index in range(45)
    }
    endpoint = _RawListEndpoint(pages)
    records, helper = await foundry_audit_cli._list_log_files(
        _LogFileClient(raw=endpoint),
        _list_args(page_size=1, batch_pages=999),
        4,
    )
    assert len(records) == 40
    assert len(endpoint.calls) == 40
    assert helper.pages_fetched == 40
    assert helper.next_page_token == "p40"


@pytest.mark.asyncio
async def test_pagination_retry_restarts_helper_without_duplicate_counts() -> None:
    calls: list[str | None] = []
    first_attempt = True

    class Endpoint:
        async def list(self, organization_rid: str, **kwargs: Any) -> _RawResponse:
            nonlocal first_attempt
            token = kwargs.get("page_token")
            calls.append(token)
            if token == "second" and first_attempt:
                first_attempt = False
                raise requests.RequestException("transient")
            page = (
                _Page([{"id": "one"}], "second")
                if token is None
                else _Page([{"id": "two"}], None)
            )
            return _RawResponse(page)

    handler = RetryHandler(
        max_retries=1,
        base_delay=0,
        jitter=False,
        timeout_s=None,
    )
    records, helper = await handler.execute(
        foundry_audit_cli._list_log_files,
        _LogFileClient(raw=Endpoint()),
        _list_args(page_size=1, batch_pages=2),
        5,
    )
    assert calls == [None, "second", None, "second"]
    assert records == [{"id": "one"}, {"id": "two"}]
    assert helper.pages_fetched == 2
    assert helper.total_items == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "succeeds"), [(503, True), (429, False)])
async def test_raw_page_retries_503_and_exhausts_429(
    status: int, succeeds: bool
) -> None:
    attempts = 0

    class Endpoint:
        async def list(self, organization_rid: str, **kwargs: Any) -> _RawResponse:
            nonlocal attempts
            attempts += 1
            if not succeeds or attempts == 1:
                raise _HttpError(status)
            return _RawResponse(_Page([{"id": "one"}], None))

    handler = RetryHandler(
        max_retries=1,
        base_delay=0,
        jitter=False,
        timeout_s=None,
    )
    invocation = handler.execute(
        foundry_audit_cli._list_log_files,
        _LogFileClient(raw=Endpoint()),
        _list_args(page_size=1),
        5,
    )
    if succeeds:
        records, helper = await invocation
        assert records == [{"id": "one"}]
        assert helper.pages_fetched == 1
    else:
        with pytest.raises(_HttpError) as exc:
            await invocation
        assert exc.value.response.status_code == 429
    assert attempts == 2


def test_metadata_only_allows_list_and_blocks_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    cfg = _Cfg()
    guard = AccessControlGuard(cfg, "AUDIT")
    guard.check("log_file", "list")
    with pytest.raises(AccessControlError) as exc:
        guard.check("log_file", "content")
    assert exc.value.exit_code == EXIT_ACCESS_CONTROL


def test_acl_operation_disable_precedes_namespace_metadata_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_AUDIT_METADATA_ONLY", "false")
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_AUDIT_LOG_FILE_LIST_ENABLED", "false")
    guard = AccessControlGuard(_Cfg(), "AUDIT")
    with pytest.raises(AccessControlError) as exc:
        guard.check("log_file", "list")
    assert exc.value.step == 1
    guard.check("log_file", "content")


def test_packaged_metadata_policy_is_cwd_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_METADATA_ONLY", "true")
    assert foundry_audit_cli._METADATA_ALLOWLIST_PATH.is_file()
    guard = AccessControlGuard(
        _Cfg(),
        "AUDIT",
        metadata_allowlist_path=str(foundry_audit_cli._METADATA_ALLOWLIST_PATH),
    )
    guard.check("log_file", "list")
    with pytest.raises(AccessControlError):
        guard.check("log_file", "content")


@pytest.mark.asyncio
async def test_acl_runs_before_factory_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = MagicMock()
    guard.check.side_effect = AccessControlError("blocked")
    factory_constructor = MagicMock(side_effect=AssertionError("factory constructed"))
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_audit_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_audit_cli,
        "AccessControlGuard",
        lambda cfg, namespace, **kwargs: guard,
    )
    monkeypatch.setattr(foundry_audit_cli, "AsyncClientFactory", factory_constructor)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "log-file", "content", "organization", "file"],
    )
    assert await foundry_audit_cli.main() == EXIT_ACCESS_CONTROL
    guard.check.assert_called_once_with("log_file", "content")
    factory_constructor.assert_not_called()


class _ByteStream:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        failure: BaseException | None = None,
        fail_after: int | None = None,
    ) -> None:
        self.chunks = chunks
        self.failure = failure
        self.fail_after = fail_after
        self.index = 0
        self.closed = False

    def __aiter__(self) -> _ByteStream:
        return self

    async def __anext__(self) -> bytes:
        if self.fail_after is not None and self.index == self.fail_after:
            assert self.failure is not None
            raise self.failure
        if self.index == len(self.chunks):
            raise StopAsyncIteration
        value = self.chunks[self.index]
        self.index += 1
        return value

    async def aclose(self) -> None:
        self.closed = True


class _PublicStreamResponse:
    def __init__(self, stream: _ByteStream) -> None:
        self.stream = stream
        self.private_accesses: list[str] = []

    def aiter_bytes(self) -> _ByteStream:
        return self.stream

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            self.private_accesses.append(name)
            raise AssertionError(f"private SDK field accessed: {name}")
        raise AttributeError(name)


class _StreamContext:
    def __init__(self, response: _PublicStreamResponse) -> None:
        self.response = response
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _PublicStreamResponse:
        self.entered = True
        return self.response

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.exited = True
        return False


class _StreamingEndpoint:
    def __init__(self, context: _StreamContext) -> None:
        self.context = context
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def content(self, *args: Any, **kwargs: Any) -> _StreamContext:
        self.calls.append((args, kwargs))
        return self.context


class _DownloadCfg(_Cfg):
    def __init__(self, root: Path, limit: int) -> None:
        self.download_path = root
        self.max_download_bytes = limit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "limit", "size", "truncated", "source", "source_at_least"),
    [
        (b"abc", 5, 3, False, 3, None),
        (b"abcde", 5, 5, False, 5, None),
        (b"abcdefghi", 5, 5, True, None, 6),
    ],
)
async def test_content_streams_unknown_length_with_one_byte_probe(
    tmp_path: Path,
    payload: bytes,
    limit: int,
    size: int,
    truncated: bool,
    source: int | None,
    source_at_least: int | None,
) -> None:
    chunks = [payload, b"must-not-be-read"] if len(payload) > limit else [payload]
    stream = _ByteStream(chunks)
    response = _PublicStreamResponse(stream)
    context = _StreamContext(response)
    endpoint = _StreamingEndpoint(context)
    result = await foundry_audit_cli._download_content(
        _LogFileClient(streaming=endpoint),
        _content_args(),
        12,
        _DownloadCfg(tmp_path, limit),
    )
    assert Path(result["file_path"]).read_bytes() == payload[:limit]
    assert result["file_size"] == size
    assert result["truncated"] is truncated
    assert result["source_size"] == source
    assert result["source_size_at_least"] == source_at_least
    assert endpoint.calls == [
        (("ri.organization.test", "log-file-id"), {"request_timeout": 12})
    ]
    assert context.entered is True and context.exited is True
    assert stream.closed is True
    assert response.private_accesses == []
    if truncated:
        assert stream.index == 1


@pytest.mark.asyncio
async def test_content_passes_all_unavailable_headers_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Result:
        def to_dict(self) -> dict[str, bool]:
            return {"ok": True}

    class Handler:
        def __init__(self, *, config: Any) -> None:
            captured["config"] = config

        async def save(self, chunks: Any, **kwargs: Any) -> Result:
            captured.update(kwargs)
            return Result()

    monkeypatch.setattr(foundry_audit_cli, "BinaryDownloadHandler", Handler)
    response = _PublicStreamResponse(_ByteStream([b"data"]))
    await foundry_audit_cli._download_content(
        _LogFileClient(streaming=_StreamingEndpoint(_StreamContext(response))),
        _content_args(output_filename=None),
        6,
        _Cfg(),
    )
    assert captured["content_length"] is None
    assert captured["content_encoding"] is None
    assert captured["mime_type"] is None
    assert captured["namespace"] == "audit"
    assert captured["operation"] == "log_file.content"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [OSError("stream failed"), asyncio.CancelledError()],
)
async def test_content_failure_or_cancellation_cleans_partial_files_and_context(
    tmp_path: Path, failure: BaseException
) -> None:
    stream = _ByteStream([b"partial"], failure=failure, fail_after=1)
    response = _PublicStreamResponse(stream)
    context = _StreamContext(response)
    with pytest.raises(type(failure)):
        await foundry_audit_cli._download_content(
            _LogFileClient(streaming=_StreamingEndpoint(context)),
            _content_args(),
            5,
            _DownloadCfg(tmp_path, 20),
        )
    assert list(tmp_path.rglob("*")) == []
    assert stream.closed is True
    assert context.exited is True


@pytest.mark.asyncio
async def test_content_retry_removes_failed_partial_before_publishing_success(
    tmp_path: Path,
) -> None:
    streams = [
        _ByteStream(
            [b"partial"],
            failure=requests.RequestException("transport"),
            fail_after=1,
        ),
        _ByteStream([b"complete"]),
    ]
    contexts: list[_StreamContext] = []

    class Endpoint:
        def content(self, *args: Any, **kwargs: Any) -> _StreamContext:
            context = _StreamContext(_PublicStreamResponse(streams[len(contexts)]))
            contexts.append(context)
            return context

    handler = RetryHandler(
        max_retries=1,
        base_delay=0,
        jitter=False,
        timeout_s=None,
    )
    result = await handler.execute(
        foundry_audit_cli._download_content,
        _LogFileClient(streaming=Endpoint()),
        _content_args(),
        5,
        _DownloadCfg(tmp_path, 20),
    )
    assert Path(result["file_path"]).read_bytes() == b"complete"
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1
    assert len(contexts) == 2
    assert all(context.exited for context in contexts)
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename", ["../escape", r"..\escape", "/absolute", "nul\x00name", ".", ".."]
)
async def test_content_rejects_unsafe_filename_without_creating_download_root(
    tmp_path: Path, filename: str
) -> None:
    root = tmp_path / "downloads"
    response = _PublicStreamResponse(_ByteStream([b"secret"]))
    context = _StreamContext(response)
    with pytest.raises(InvalidDownloadError):
        await foundry_audit_cli._download_content(
            _LogFileClient(streaming=_StreamingEndpoint(context)),
            _content_args(output_filename=filename),
            5,
            _DownloadCfg(root, 20),
        )
    assert root.exists() is False
    assert context.exited is True


@pytest.mark.asyncio
async def test_main_list_outputs_data_then_pagination_metadata_on_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    endpoint = _RawListEndpoint({None: _Page([{"id": "log-1"}], "cursor")})
    client = _LogFileClient(raw=endpoint)
    factory = _Factory(client)
    guard = _patch_main_dependencies(monkeypatch, factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "log-file",
            "list",
            "organization",
            "--start-date",
            "2026-08-01",
            "--page-size",
            "1",
            "--format",
            "json",
        ],
    )
    assert await foundry_audit_cli.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [{"id": "log-1"}]
    assert "next_page_token" in captured.err
    assert "cursor" in captured.err
    assert "log-1" not in captured.err
    guard.check.assert_called_once_with("log_file", "list")


@pytest.mark.asyncio
async def test_main_content_forces_json_and_orders_acl_scope_client_retry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    client = _LogFileClient()
    factory = _Factory(client, events)
    guard = MagicMock()
    guard.check.side_effect = lambda *args: events.append("acl")
    retry = MagicMock()
    retry.execute = AsyncMock(return_value={"file_path": "safe.bin", "file_size": 4})
    _patch_main_dependencies(monkeypatch, factory, retry=retry, guard=guard)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "log-file",
            "content",
            "organization",
            "file",
            "--format",
            "toon",
        ],
    )
    assert await foundry_audit_cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "file_path": "safe.bin",
        "file_size": 4,
    }
    assert events == ["acl", "scope-enter", "client-create", "scope-exit"]
    assert retry.execute.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("cli_timeout", "config_timeout"), [(17, 42), (None, 42)])
async def test_main_passes_selected_timeout_to_retry_and_sdk(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_timeout: int | None,
    config_timeout: int,
) -> None:
    class Cfg(_Cfg):
        timeout_s = config_timeout

    endpoint = _RawListEndpoint({None: _Page([], None)})
    factory = _Factory(_LogFileClient(raw=endpoint))
    captured: dict[str, Any] = {}

    def retry_factory(**kwargs: Any) -> _ImmediateRetry:
        captured.update(kwargs)
        return _ImmediateRetry()

    _patch_main_dependencies(monkeypatch, factory, cfg_type=Cfg)
    monkeypatch.setattr(foundry_audit_cli, "RetryHandler", retry_factory)
    argv = [
        "prog",
        "log-file",
        "list",
        "organization",
        "--start-date",
        "2026-08-01",
    ]
    if cli_timeout is not None:
        argv.extend(["--timeout", str(cli_timeout)])
    monkeypatch.setattr(sys, "argv", argv)
    assert await foundry_audit_cli.main() == 0
    capsys.readouterr()
    expected = cli_timeout if cli_timeout is not None else config_timeout
    assert captured == {"timeout_s": expected}
    assert endpoint.calls[0][1]["request_timeout"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, 3601])
async def test_main_rejects_timeout_before_acl_or_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    timeout: int,
) -> None:
    guard_constructor = MagicMock()
    factory_constructor = MagicMock()
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_audit_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(foundry_audit_cli, "AccessControlGuard", guard_constructor)
    monkeypatch.setattr(foundry_audit_cli, "AsyncClientFactory", factory_constructor)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "log-file",
            "content",
            "organization",
            "file",
            "--timeout",
            str(timeout),
        ],
    )
    assert await foundry_audit_cli.main() == EXIT_USER_INPUT
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_USER_INPUT
    guard_constructor.assert_not_called()
    factory_constructor.assert_not_called()


@pytest.mark.asyncio
async def test_main_invalid_date_stops_before_acl_and_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    guard_constructor = MagicMock()
    factory_constructor = MagicMock()
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_audit_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(foundry_audit_cli, "AccessControlGuard", guard_constructor)
    monkeypatch.setattr(foundry_audit_cli, "AsyncClientFactory", factory_constructor)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "log-file", "list", "organization", "--start-date", "20260801"],
    )
    assert await foundry_audit_cli.main() == EXIT_USER_INPUT
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_USER_INPUT
    guard_constructor.assert_not_called()
    factory_constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (_HttpError(401), EXIT_AUTH),
        (_HttpError(403), EXIT_PERMISSION_DENIED),
        (_HttpError(404), EXIT_NOT_FOUND),
        (TimeoutError("timed out"), EXIT_TIMEOUT),
        (_HttpError(503), EXIT_SERVER_ERROR),
        (_HttpError(429), EXIT_RATE_LIMIT),
        (ConfigurationError("bad config"), EXIT_CONFIGURATION),
        (RuntimeError("unexpected"), EXIT_SERVER_ERROR),
    ],
)
async def test_main_serializes_exact_adr_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exception: Exception,
    expected: int,
) -> None:
    retry = MagicMock()
    retry.execute = AsyncMock(side_effect=exception)
    _patch_main_dependencies(
        monkeypatch,
        _Factory(_LogFileClient()),
        retry=retry,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "log-file", "content", "organization", "file"],
    )
    assert await foundry_audit_cli.main() == expected
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["error"] is True
    assert envelope["exit_code"] == expected


@pytest.mark.asyncio
async def test_main_maps_async_cancellation_to_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    retry = MagicMock()
    retry.execute = AsyncMock(side_effect=asyncio.CancelledError())
    _patch_main_dependencies(
        monkeypatch,
        _Factory(_LogFileClient()),
        retry=retry,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "log-file", "content", "organization", "file"],
    )
    assert await foundry_audit_cli.main() == EXIT_TIMEOUT
    assert json.loads(capsys.readouterr().out)["exit_code"] == EXIT_TIMEOUT


class _TracingCfg(_Cfg):
    def __init__(self, enabled: bool) -> None:
        self.enable_tracing = enabled


class _TracingFactory:
    def __init__(self, client: _LogFileClient, headers: list[dict[str, str]]) -> None:
        self.client = client
        self.headers = headers

    def invocation_scope(self, cfg: _TracingCfg) -> Any:
        return TracingProvider(config=cfg).scope()

    def create(self, cfg: _TracingCfg) -> Any:
        from foundry_sdk._core.http_client import _prepare_client_data

        self.headers.append(dict(_prepare_client_data(None)[2]))
        return SimpleNamespace(
            audit=SimpleNamespace(
                Organization=SimpleNamespace(LogFile=self.client),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_b3_transport_headers_enabled_disabled_retry_stable_and_restored(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    enabled: bool,
) -> None:
    from foundry_sdk import SAMPLED_VAR, SPAN_ID_VAR, TRACE_ID_VAR
    from foundry_sdk._core.http_client import _prepare_client_data

    for env_name in ("FOUNDRY_TRACE_ID", "FOUNDRY_SPAN_ID", "FOUNDRY_SAMPLED"):
        monkeypatch.delenv(env_name, raising=False)
    prior_trace = TRACE_ID_VAR.set(None)
    prior_span = SPAN_ID_VAR.set(None)
    prior_sampled = SAMPLED_VAR.set(None)
    headers: list[dict[str, str]] = []
    attempts = 0

    class Endpoint:
        async def list(self, organization_rid: str, **kwargs: Any) -> _RawResponse:
            nonlocal attempts
            attempts += 1
            headers.append(dict(_prepare_client_data(None)[2]))
            if attempts == 1:
                raise requests.RequestException("retry")
            return _RawResponse(_Page([{"id": "one"}], None))

    factory = _TracingFactory(_LogFileClient(raw=Endpoint()), headers)
    retry = RetryHandler(
        max_retries=1,
        base_delay=0,
        jitter=False,
        timeout_s=None,
    )
    cfg = _TracingCfg(enabled)
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", lambda: cfg)
    monkeypatch.setattr(foundry_audit_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_audit_cli,
        "AccessControlGuard",
        lambda cfg, namespace, **kwargs: MagicMock(),
    )
    monkeypatch.setattr(foundry_audit_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(foundry_audit_cli, "RetryHandler", lambda **kwargs: retry)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "log-file", "list", "organization", "--start-date", "2026-08-01"],
    )
    try:
        assert await foundry_audit_cli.main() == 0
        capsys.readouterr()
        assert attempts == 2
        assert len(headers) == 3
        for outbound_headers in headers:
            assert "traceparent" not in {key.lower() for key in outbound_headers}
            assert "tracestate" not in {key.lower() for key in outbound_headers}
        if enabled:
            b3 = [
                (
                    item["X-B3-TraceId"],
                    item["X-B3-SpanId"],
                    item["X-B3-Sampled"],
                )
                for item in headers
            ]
            assert b3[0] == b3[1] == b3[2]
            assert len(b3[0][0]) == 32
            assert len(b3[0][1]) == 16
            assert b3[0][2] == "1"
        else:
            for item in headers:
                assert not any(key.startswith("X-B3-") for key in item)
        assert TRACE_ID_VAR.get() is None
        assert SPAN_ID_VAR.get() is None
        assert SAMPLED_VAR.get() is None
    finally:
        SAMPLED_VAR.reset(prior_sampled)
        SPAN_ID_VAR.reset(prior_span)
        TRACE_ID_VAR.reset(prior_trace)


@pytest.mark.asyncio
async def test_b3_scope_restores_prior_values_after_formatter_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from foundry_sdk import SAMPLED_VAR, SPAN_ID_VAR, TRACE_ID_VAR

    trace_token = TRACE_ID_VAR.set("a" * 32)
    span_token = SPAN_ID_VAR.set("b" * 16)
    sampled_token = SAMPLED_VAR.set("0")
    cfg = _TracingCfg(True)
    factory = _TracingFactory(_LogFileClient(), [])
    _patch_main_dependencies(monkeypatch, factory, cfg_type=_Cfg)
    monkeypatch.setattr(foundry_audit_cli, "ConfigLoader", lambda: cfg)
    formatter = MagicMock()
    formatter.format.side_effect = RuntimeError("formatter failed")
    monkeypatch.setattr(foundry_audit_cli, "OutputFormatter", lambda **kwargs: formatter)
    retry = MagicMock()
    retry.execute = AsyncMock(return_value=([], MagicMock()))
    monkeypatch.setattr(foundry_audit_cli, "RetryHandler", lambda **kwargs: retry)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "log-file", "list", "organization", "--start-date", "2026-08-01"],
    )
    try:
        assert await foundry_audit_cli.main() == EXIT_SERVER_ERROR
        assert TRACE_ID_VAR.get() == "a" * 32
        assert SPAN_ID_VAR.get() == "b" * 16
        assert SAMPLED_VAR.get() == "0"
    finally:
        SAMPLED_VAR.reset(sampled_token)
        SPAN_ID_VAR.reset(span_token)
        TRACE_ID_VAR.reset(trace_token)


def test_source_never_uses_eager_content_private_sdk_fields_or_w3c() -> None:
    source = Path(foundry_audit_cli.__file__).read_text(encoding="utf-8")
    assert "._response" not in source
    assert "client.content(" not in source
    assert "traceparent" not in source.lower()
    assert "tracestate" not in source.lower()
