"""Third-party CLI contracts through native SDK clients and offline HTTP."""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import httpx
import pytest

from pal_found_cli.common.log_setup import METADATA_SEPARATOR
from pal_found_cli.third_party_applications.scripts import (
    pal_found_third_party_applications_cli as cli,
)

RID = (
    "ri.third-party-applications.main.application.00000000-0000-0000-0000-000000000001"
)
BASE = f"/api/v2/thirdPartyApplications/{RID}"
VERSIONS = BASE + "/website/versions"
BINARY = b"PK\x03\x04\x00\xffoffline website\x00"
CASES = [
    (["third-party-application", "get", RID], "GET", BASE, {"rid": RID}, {}, None),
    (
        ["website", "deploy", RID, "--version", "1.2.0"],
        "POST",
        BASE + "/website/deploy",
        {"deployedVersion": "1.2.0", "subdomains": ["offline"]},
        {},
        {"version": "1.2.0"},
    ),
    (
        ["website", "get", RID],
        "GET",
        BASE + "/website",
        {"deployedVersion": "1.2.0", "subdomains": ["offline"]},
        {},
        None,
    ),
    (
        ["website", "undeploy", RID],
        "POST",
        BASE + "/website/undeploy",
        {"subdomains": ["offline"]},
        {},
        None,
    ),
    (
        ["version", "delete", RID, "1.2.0"],
        "DELETE",
        VERSIONS + "/1.2.0",
        None,
        {},
        None,
    ),
    (
        ["version", "get", RID, "1.2.0"],
        "GET",
        VERSIONS + "/1.2.0",
        {"version": "1.2.0"},
        {},
        None,
    ),
    (
        ["version", "list", RID, "--page-size", "7"],
        "GET",
        VERSIONS,
        {"data": [{"version": "1.2.0"}], "nextPageToken": "next"},
        {"pageSize": "7"},
        None,
    ),
    (
        ["version", "upload", RID, "--version", "1.2.0", "--file", "website.zip"],
        "POST",
        VERSIONS + "/upload",
        {"version": "1.2.0"},
        {"version": "1.2.0"},
        BINARY,
    ),
    (
        [
            "version",
            "upload-snapshot",
            RID,
            "--version",
            "1.2.0-snapshot.1",
            "--file",
            "website.zip",
            "--snapshot-identifier",
            "build-42",
        ],
        "POST",
        VERSIONS + "/uploadSnapshot",
        {"version": "1.2.0-snapshot.1"},
        {"version": "1.2.0-snapshot.1", "snapshotIdentifier": "build-42"},
        BINARY,
    ),
]


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Keep production config, guard, factory and SDK; replace HTTP transport only."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "empty.env").write_text("")
    (tmp_path / "website.zip").write_bytes(BINARY)
    for key in list(os.environ):
        if key.startswith("FOUNDRY_") or key.startswith("B3_"):
            monkeypatch.delenv(key)
    for key, value in {
        "FOUNDRY_AGENTIC_CLI_ENV_FILE": str(tmp_path / "empty.env"),
        "FOUNDRY_HOSTNAME": "https://offline.invalid",
        "FOUNDRY_TOKEN": "offline-fake-token",
        "FOUNDRY_AGENTIC_CLI_READONLY": "false",
        "FOUNDRY_AGENTIC_CLI_DEFAULT_FORMAT": "json",
        "FOUNDRY_AGENTIC_CLI_LOG_LEVEL": "CRITICAL",
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


async def invoke(monkeypatch, capsys, argv, respond):
    requests = []

    def handler(request):
        requests.append(request)
        return respond(request, len(requests))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        monkeypatch.setattr(
            "foundry_sdk._core.api_client.AsyncHttpClient",
            lambda *args, **kwargs: transport,
        )
        monkeypatch.setattr(sys, "argv", ["pal-found-third-party-applications", *argv])
        exit_code = await cli.main()
    output = capsys.readouterr()
    return exit_code, json.loads(output.out), output.err, requests


@pytest.mark.parametrize("argv,method,path,response,query,body", CASES)
async def test_all_native_commands(
    offline, monkeypatch, capsys, argv, method, path, response, query, body
):
    code, output, stderr, requests = await invoke(
        monkeypatch,
        capsys,
        [*argv, "--timeout", "7"],
        lambda r, n: httpx.Response(204)
        if response is None
        else httpx.Response(200, json=response),
    )
    assert code == 0, output
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == (method, path)
    assert dict(request.url.params) == query
    assert request.headers["authorization"] == "Bearer offline-fake-token"
    assert request.extensions["timeout"]["read"] == 7
    if isinstance(body, dict):
        assert json.loads(request.content) == body
    else:
        assert request.content == (body or b"")
    if body == BINARY:
        assert request.headers["content-type"] == "application/octet-stream"
    if "data" in (response or {}):
        assert output == response["data"]
        assert metadata(stderr)["next_page_token"] == "next"
    elif response is None:
        assert output is None
    else:
        assert all(output[key] == value for key, value in response.items())


def metadata(stderr):
    return json.loads(stderr.split(METADATA_SEPARATOR)[-1].strip())


@pytest.mark.parametrize(
    "flags,count,start",
    [
        ([], 1, 0),
        (["--page-token", "cursor-5"], 1, 5),
        (["--max-pages", "2"], 2, 0),
        (["--all", "--max-pages", "1"], 40, 0),
    ],
)
async def test_native_paging_bounds_and_cursors(
    offline, monkeypatch, capsys, flags, count, start
):
    def respond(request, n):
        current = int(request.url.params.get("pageToken", "cursor-0").split("-")[-1])
        return httpx.Response(
            200,
            json={
                "data": [{"version": str(current)}],
                "nextPageToken": f"cursor-{current + 1}",
            },
        )

    code, output, stderr, requests = await invoke(
        monkeypatch,
        capsys,
        ["version", "list", RID, "--page-size", "3", *flags],
        respond,
    )
    assert code == 0
    assert output == [{"version": str(i)} for i in range(start, start + count)]
    assert [dict(r.url.params) for r in requests] == [
        {"pageSize": "3", **({"pageToken": f"cursor-{i}"} if i else {})}
        for i in range(start, start + count)
    ]
    assert metadata(stderr) == {
        "pages_fetched": count,
        "total_items": count,
        "page_size": 3,
        "next_page_token": f"cursor-{start + count}",
    }


async def test_interrupted_page_retries_whole_batch_without_duplicate_output(
    offline, monkeypatch, capsys
):
    async def no_delay(*args):
        pass

    monkeypatch.setattr("pal_found_cli.common.retry.asyncio.sleep", no_delay)

    def respond(request, n):
        if n == 2:
            raise httpx.ReadTimeout("offline interrupted page", request=request)
        cursor = request.url.params.get("pageToken")
        return httpx.Response(
            200,
            json={
                "data": [{"version": "2" if cursor else "1"}],
                **({} if cursor else {"nextPageToken": "next"}),
            },
        )

    code, output, stderr, requests = await invoke(
        monkeypatch, capsys, ["version", "list", RID, "--max-pages", "2"], respond
    )
    assert code == 0
    assert output == [{"version": "1"}, {"version": "2"}]
    assert [r.url.params.get("pageToken") for r in requests] == [
        None,
        "next",
        None,
        "next",
    ]
    assert metadata(stderr) == {"pages_fetched": 2, "total_items": 2, "page_size": 100}


@pytest.mark.parametrize("status,code", [(401, 2), (403, 3), (404, 4)])
async def test_nested_native_sdk_errors(offline, monkeypatch, capsys, status, code):
    result, output, stderr, requests = await invoke(
        monkeypatch,
        capsys,
        ["website", "get", RID],
        lambda r, n: httpx.Response(
            status,
            json={
                "errorCode": "NOT_FOUND",
                "errorName": "OfflineError",
                "errorInstanceId": "offline",
                "parameters": {},
            },
        ),
    )
    assert result == code
    assert output["error"] is True
    assert output["http_status"] == status
    assert len(requests) == 1


@pytest.mark.parametrize(
    "argv,code,readonly",
    [
        (["website", "deploy", RID], 1, False),
        (["website", "get", ""], 1, False),
        (["website", "get", RID, "--timeout", "0"], 1, False),
        (["website", "get", RID, "--format", "xml"], 1, False),
        (["version", "list", RID, "--page-size", "0"], 1, False),
        (["version", "list", RID, "--max-pages", "-1"], 1, False),
        (
            ["version", "upload", RID, "--version", "1", "--file", "absent.zip"],
            1,
            False,
        ),
        (
            ["version", "upload", RID, "--version", "1", "--file", "oversized.zip"],
            1,
            False,
        ),
        (["website", "deploy", RID, "--version", "1"], 8, True),
        (["version", "upload", RID, "--version", "1", "--file", "absent.zip"], 8, True),
    ],
)
async def test_denial_before_http(offline, monkeypatch, capsys, argv, code, readonly):
    if "oversized.zip" in argv:
        with Path("oversized.zip").open("wb") as stream:
            stream.truncate(cli._UPLOAD_MAX_BYTES + 1)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", str(readonly).lower())
    result, output, stderr, requests = await invoke(
        monkeypatch,
        capsys,
        argv,
        lambda r, n: pytest.fail("Denied operation reached HTTP"),
    )
    assert result == code
    assert output["error"] is True
    assert requests == []


async def test_missing_credentials_before_http(offline, monkeypatch, capsys):
    monkeypatch.delenv("FOUNDRY_TOKEN")
    code, output, stderr, requests = await invoke(
        monkeypatch,
        capsys,
        ["website", "get", RID],
        lambda r, n: pytest.fail("Missing credentials reached HTTP"),
    )
    assert code == 9
    assert output["error"] is True
    assert requests == []


def test_native_network_denial_is_active(offline):
    with pytest.raises(AssertionError, match="Native network and DNS denied"):
        socket.getaddrinfo("example.com", 443)
    with pytest.raises(AssertionError, match="Native network and DNS denied"):
        httpx.get("https://example.com")
