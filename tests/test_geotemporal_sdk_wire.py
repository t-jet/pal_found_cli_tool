"""Retained Geotemporal CLI contracts through native SDK transport, offline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import toon_format
from foundry_sdk import ATTRIBUTION_VAR, SAMPLED_VAR, SPAN_ID_VAR, TRACE_ID_VAR
from foundry_sdk._core.http_client import AsyncHttpClient

from pal_found_cli.ontologies import _geotemporal_compat as compat
from pal_found_cli.ontologies import _legacy_geotemporal_models as legacy
from pal_found_cli.ontologies.scripts import pal_found_ontologies_cli as cli

LATEST = "get-geotemporal-series-latest-value"
HISTORIC = "stream-geotemporal-series-historic-values"
ARGS = ["ont/o", "Object Type", "pk/with ?#%", "geo/property"]
PREFIX = (
    "/api/v2/ontologies/ont%2Fo/objects/Object%20Type/pk%2Fwith%20%3F%23%25"
    "/geotemporalSeries/geo%2Fproperty/"
)
POINT = {
    "time": "2026-11-17T12:00:00Z",
    "position": {"type": "Point", "coordinates": [23.3, 42.7]},
}
BINARY = b"\x00historic\xff\n"


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "empty.env"
    empty.write_text("")
    for key in list(os.environ):
        if key.startswith(("FOUNDRY_", "B3_")):
            monkeypatch.delenv(key)
    for key, value in {
        "FOUNDRY_AGENTIC_CLI_ENV_FILE": str(empty),
        "FOUNDRY_HOSTNAME": "https://offline.invalid",
        "FOUNDRY_TOKEN": "offline-fake-token",
        "FOUNDRY_AGENTIC_CLI_ENABLED": "true",
        "FOUNDRY_AGENTIC_CLI_READONLY": "false",
        "FOUNDRY_AGENTIC_CLI_METADATA_ONLY": "false",
        "FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT": "json",
        "FOUNDRY_AGENTIC_CLI_LOG_LEVEL": "CRITICAL",
        "FOUNDRY_AGENTIC_CLI_DOWNLOAD_PATH": str(tmp_path / "downloads"),
        "FOUNDRY_AGENTIC_CLI_SESSION_PATH": str(tmp_path / "sessions"),
        "FOUNDRY_AGENTIC_CLI_RETRY_MAX_ATTEMPTS": "2",
        "FOUNDRY_AGENTIC_CLI_RETRY_INITIAL_DELAY_MS": "0",
        "FOUNDRY_AGENTIC_CLI_RETRY_MAX_DELAY_MS": "0",
    }.items():
        monkeypatch.setenv(key, value)

    def denied(*args, **kwargs):
        raise AssertionError("Native network and DNS denied")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    original_connect = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return original_connect(sock, address)
        return denied(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied)
    return tmp_path


async def invoke(monkeypatch, capsys, operation, respond, flags=(), *, output="json"):
    requests = []

    async def handler(request):
        requests.append(request)
        result = respond(request, len(requests))
        return await result if hasattr(result, "__await__") else result

    clients = []
    original_init = AsyncHttpClient.__init__

    def track_init(client, *args, **kwargs):
        original_init(client, *args, **kwargs)
        clients.append(client)

    monkeypatch.setattr(AsyncHttpClient, "__init__", track_init)
    monkeypatch.setattr(
        "foundry_sdk._core.http_client._get_async_transport",
        lambda *a, **kw: httpx.MockTransport(handler),
    )
    try:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "pal-found-ontologies",
                "geotemporal-series-property",
                operation,
                *ARGS,
                *flags,
                "--format",
                output,
            ],
        )
        code = await cli.main()
    finally:
        for client in clients:
            await client.aclose()
    captured = capsys.readouterr()
    # The shared formatter falls back to JSON for unsupported TOON values,
    # including the legacy model's datetime object.
    result = (
        json.loads(captured.out)
        if output == "json" or captured.out.lstrip().startswith("{")
        else toon_format.loads(captured.out)
    )
    return code, result, requests


@pytest.mark.parametrize("output", ["json", "toon"])
@pytest.mark.parametrize("payload", [POINT, None])
async def test_latest_native_wire_and_output(
    offline, monkeypatch, capsys, payload, output
):
    code, result, requests = await invoke(
        monkeypatch,
        capsys,
        LATEST,
        lambda r, n: httpx.Response(200, json=payload),
        [
            "--sdk-package-rid",
            "ri.foundry.main.sdk.package",
            "--sdk-version",
            "1.2+test",
            "--timeout",
            "7",
        ],
        output=output,
    )
    assert code == 0, result
    assert len(requests) == 1
    req = requests[0]
    assert (
        req.method == "GET"
        and req.url.raw_path.decode().split("?")[0] == PREFIX + "latestValue"
    )
    assert dict(req.url.params) == {
        "sdkPackageRid": "ri.foundry.main.sdk.package",
        "sdkVersion": "1.2+test",
    }
    assert req.content == b"" and req.headers["accept"] == "application/json"
    assert req.headers["authorization"] == "Bearer offline-fake-token"
    assert req.extensions["timeout"]["read"] == 7
    if payload is None:
        assert result is None
    else:
        assert result["position"]["coordinates"] == [23.3, 42.7]
        assert datetime.fromisoformat(result["time"]) == datetime.fromisoformat(
            POINT["time"]
        )


@pytest.mark.parametrize(
    "range_value",
    [
        None,
        "null",
        {"type": "absolute"},
        {
            "type": "absolute",
            "startTime": "2026-11-01T00:00:00Z",
            "endTime": "2026-11-02T00:00:00Z",
        },
        {
            "type": "relative",
            "startTime": {"when": "BEFORE", "value": 3, "unit": "DAYS"},
        },
    ],
)
async def test_historic_body_and_download(offline, monkeypatch, capsys, range_value):
    flags = (
        []
        if range_value is None
        else [
            "--range",
            range_value if isinstance(range_value, str) else json.dumps(range_value),
        ]
    )
    code, result, requests = await invoke(
        monkeypatch,
        capsys,
        HISTORIC,
        lambda r, n: httpx.Response(200, content=BINARY),
        flags,
    )
    assert code == 0, result
    assert len(requests) == 1
    req = requests[0]
    assert (
        req.method == "POST"
        and req.url.raw_path.decode() == PREFIX + "streamHistoricValues"
    )
    assert (
        req.headers["accept"] == "*/*"
        and req.headers["content-type"] == "application/json"
    )
    assert dict(req.url.params) == {}
    expected_range = range_value
    if (
        isinstance(range_value, dict)
        and "startTime" in range_value
        and range_value["type"] == "absolute"
    ):
        expected_range = {
            **range_value,
            "startTime": "2026-11-01T00:00:00+00:00",
            "endTime": "2026-11-02T00:00:00+00:00",
        }
    assert json.loads(req.content) == (
        {} if range_value in (None, "null") else {"range": expected_range}
    )
    stored = Path(result["file_path"])
    assert stored.read_bytes() == BINARY and stored.is_relative_to(
        offline / "downloads"
    )
    assert result["file_size"] == len(BINARY) and result["truncated"] is False
    assert result["checksum_sha256"] == hashlib.sha256(BINARY).hexdigest()


@pytest.mark.parametrize(
    "bad_range",
    [
        {"type": "unknown"},
        {"type": "absolute", "startTime": "2026-11-01T00:00:00"},
        {
            "type": "relative",
            "startTime": {"when": "BEFORE", "value": 1, "unit": "BANANAS"},
        },
    ],
)
async def test_invalid_legacy_range_fails_before_http(
    offline, monkeypatch, capsys, bad_range
):
    code, result, requests = await invoke(
        monkeypatch,
        capsys,
        HISTORIC,
        lambda r, n: httpx.Response(200, content=BINARY),
        ["--range", json.dumps(bad_range)],
    )
    assert code == 1 and requests == []
    assert "error" in result or "message" in result


@pytest.mark.parametrize("operation", [LATEST, HISTORIC])
@pytest.mark.parametrize("tier", ["global", "namespace", "operation", "metadata"])
async def test_access_denied_before_factory(
    offline, monkeypatch, capsys, operation, tier
):
    key = {
        "global": "FOUNDRY_AGENTIC_CLI_ENABLED",
        "namespace": "FOUNDRY_AGENTIC_CLI_ONTOLOGIES_ENABLED",
        "operation": "FOUNDRY_AGENTIC_CLI_ONTOLOGIES_GEOTEMPORAL_SERIES_PROPERTY_"
        + operation.replace("-", "_").upper()
        + "_ENABLED",
        "metadata": "FOUNDRY_AGENTIC_CLI_METADATA_ONLY",
    }[tier]
    monkeypatch.setenv(key, "true" if tier == "metadata" else "false")

    def forbidden_factory(*args, **kwargs):
        raise AssertionError("SDK constructed before access denial")

    monkeypatch.setattr(cli.AsyncClientFactory, "create", forbidden_factory)
    code, result, requests = await invoke(
        monkeypatch, capsys, operation, lambda r, n: httpx.Response(200, json=POINT)
    )
    assert code == 8 and requests == []
    assert result["error"] is True and result["exception_type"] == "AccessControlError"


@pytest.mark.parametrize("operation", [LATEST, HISTORIC])
@pytest.mark.parametrize("status", [400, 403, 404, 409])
async def test_general_sdk_errors(offline, monkeypatch, capsys, operation, status):
    code, result, requests = await invoke(
        monkeypatch,
        capsys,
        operation,
        lambda r, n: httpx.Response(
            status,
            json={
                "errorCode": "INVALID_ARGUMENT",
                "errorName": "OfflineGeneralError",
                "errorInstanceId": "00000000-0000-0000-0000-000000000001",
                "parameters": {},
            },
        ),
    )
    assert code != 0 and len(requests) == 1
    assert result["exception_type"] != "AttributeError"


async def test_historic_download_limit(offline, monkeypatch, capsys):
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_MAX_DOWNLOAD_BYTES", "3")
    code, result, _ = await invoke(
        monkeypatch, capsys, HISTORIC, lambda r, n: httpx.Response(200, content=BINARY)
    )
    assert code == 0 and result["truncated"] is True
    assert Path(result["file_path"]).read_bytes() == BINARY[:3]
    assert result["checksum_sha256"] == hashlib.sha256(BINARY[:3]).hexdigest()


@pytest.mark.parametrize("operation", [LATEST, HISTORIC])
async def test_readonly_preserves_both_read_operations(
    offline, monkeypatch, capsys, operation
):
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    code, _, requests = await invoke(
        monkeypatch,
        capsys,
        operation,
        lambda r, n: httpx.Response(200, content=BINARY)
        if operation == HISTORIC
        else httpx.Response(200, json=POINT),
    )
    assert code == 0 and len(requests) == 1


async def test_transport_retry_retains_same_wire_contract(offline, monkeypatch, capsys):
    calls = []
    real_invoke = cli._invoke

    async def tracked_invoke(*args, **kwargs):
        calls.append(True)
        return await real_invoke(*args, **kwargs)

    monkeypatch.setattr(cli, "_invoke", tracked_invoke)

    def respond(request, count):
        if count == 1:
            raise httpx.ReadTimeout("offline interrupted response", request=request)
        return httpx.Response(200, json=POINT)

    code, _, requests = await invoke(monkeypatch, capsys, LATEST, respond)
    assert code == 0 and len(requests) == 2 and len(calls) == 2
    assert requests[0].url == requests[1].url


async def test_cli_timeout_cancels_native_request(offline, monkeypatch, capsys):
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_MAX_ATTEMPTS", "1")
    closed = asyncio.Event()

    async def respond(request, count):
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    code, _, requests = await invoke(
        monkeypatch, capsys, HISTORIC, respond, ["--timeout", "1"]
    )
    assert code == 5 and len(requests) == 1 and closed.is_set()
    assert not list((offline / "downloads").rglob("*"))


async def test_download_publish_failure_removes_partial_files(
    offline, monkeypatch, capsys
):
    def fail_publish(*args, **kwargs):
        raise OSError("offline atomic publication failure")

    monkeypatch.setattr(
        "pal_found_cli.common.binary_download_handler.os.replace", fail_publish
    )
    code, _, requests = await invoke(
        monkeypatch, capsys, HISTORIC, lambda r, n: httpx.Response(200, content=BINARY)
    )
    assert code != 0 and len(requests) == 1
    assert not [p for p in (offline / "downloads").rglob("*") if p.is_file()]


@pytest.mark.parametrize("operation", [LATEST, HISTORIC])
@pytest.mark.parametrize("status", [200, 403])
def test_public_console_restores_outer_sdk_context(
    offline, monkeypatch, capsys, operation, status
):
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ENABLE_TRACING", "true")
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_ENABLE_ATTRIBUTION", "true")
    monkeypatch.setenv(
        "FOUNDRY_AGENTIC_CLI_ATTRIBUTION_RIDS", "ri.foundry.main.dataset.offline"
    )
    variables = (TRACE_ID_VAR, SPAN_ID_VAR, SAMPLED_VAR, ATTRIBUTION_VAR)
    prior = ("1" * 32, "2" * 16, "0", ["ri.foundry.main.dataset.outer"])
    tokens = [var.set(value) for var, value in zip(variables, prior)]
    observed = []

    def respond(request):
        observed.append((request, tuple(var.get() for var in variables)))
        if status == 403:
            return httpx.Response(
                403,
                json={
                    "errorCode": "PERMISSION_DENIED",
                    "errorName": "OfflineDenied",
                    "errorInstanceId": "00000000-0000-0000-0000-000000000001",
                    "parameters": {},
                },
            )
        return (
            httpx.Response(200, content=BINARY)
            if operation == HISTORIC
            else httpx.Response(200, json=POINT)
        )

    clients = []
    original_init = AsyncHttpClient.__init__

    def track_init(client, *args, **kwargs):
        original_init(client, *args, **kwargs)
        clients.append(client)

    monkeypatch.setattr(AsyncHttpClient, "__init__", track_init)
    monkeypatch.setattr(
        "foundry_sdk._core.http_client._get_async_transport",
        lambda *a, **kw: httpx.MockTransport(respond),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pal-found-ontologies",
            "geotemporal-series-property",
            operation,
            *ARGS,
            "--format",
            "json",
        ],
    )
    try:
        code = cli.console_main()
        assert (code == 0) if status == 200 else (code != 0)
        assert tuple(var.get() for var in variables) == prior
        assert len(observed) == 1
        request, during = observed[0]
        assert during[0] != prior[0] and during[1] != prior[1]
        assert during[3] == ["ri.foundry.main.dataset.offline"]
        assert request.headers["x-b3-traceid"] == during[0]
        assert request.headers["x-b3-spanid"] == during[1]
        assert request.headers["attribution"] == "ri.foundry.main.dataset.offline"
        assert "traceparent" not in request.headers
    finally:
        for client in clients:
            asyncio.run(client.aclose())
        for var, token in zip(variables, tokens):
            var.reset(token)
        capsys.readouterr()


async def test_cancellation_propagates_without_download(offline, monkeypatch, capsys):
    entered = asyncio.Event()
    completed = asyncio.Event()

    async def respond(request, count):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            completed.set()

    task = asyncio.create_task(invoke(monkeypatch, capsys, HISTORIC, respond))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
    assert not list((offline / "downloads").rglob("*"))


def test_native_method_selected_without_adapter(monkeypatch):
    def native(*args, **kwargs):
        return "native"

    resource = SimpleNamespace(get_geotemporal_series_latest_value=native)
    assert (
        compat.resolve_method(resource, "get_geotemporal_series_latest_value") is native
    )
    with pytest.raises(AttributeError):
        compat.resolve_method(resource, "different_operation")


def test_legacy_latest_validation_retains_typed_point():
    point = legacy.GeotemporalSeriesEntry.model_validate(POINT)
    assert point.time.tzinfo is not None and isinstance(point.position, legacy.GeoPoint)
    with pytest.raises(ValueError):
        legacy.GeotemporalSeriesEntry.model_validate(
            {**POINT, "position": {"coordinates": [1]}}
        )
