"""Exercise Arrow downloads through real SDK validation and HTTP serialization."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from foundry_sdk import UserTokenAuth, _core
from foundry_sdk.v2.sql_queries.sql_query import AsyncSqlQueryClient

from pal_found_cli.sql_queries.scripts import pal_found_sql_queries_cli as cli


PARAMETERS = {
    "type": "namedParameterMapping",
    "mapping": {"region": {"type": "string", "value": "west"}},
}


@pytest.fixture
async def sql_transport(monkeypatch):
    requests = []
    payload = b"arrow-fixture"

    def deny_network(*args, **kwargs):
        raise AssertionError("Public network is disabled")

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=payload)

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        monkeypatch.setattr(
            "foundry_sdk._core.api_client.AsyncHttpClient", lambda *args, **kwargs: transport
        )
        client = AsyncSqlQueryClient(
            auth=UserTokenAuth("offline"),
            hostname="https://offline.invalid",
            config=_core.Config(max_retries=0),
        )
        yield client, requests, payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options,expected,limit",
    [
        ([], {"query": "select 1"}, 100),
        (
            ["--dry-run", "--parameters-json", json.dumps(PARAMETERS), "--row-limit", "7"],
            {"query": "select 1", "dryRun": True, "parameters": PARAMETERS, "rowLimit": 7},
            100,
        ),
        ([], {"query": "select 1"}, 4),
    ],
)
async def test_execute_ontology_forwards_query_and_downloads(
    sql_transport, tmp_path, options, expected, limit
):
    client, requests, payload = sql_transport
    args = cli.build_parser().parse_args(
        ["query", "execute-ontology", "--query", "select 1", *options]
    )
    spec = cli._spec_for("sql_query", "execute_ontology")
    cli._validate_inputs(spec, args)
    cfg = SimpleNamespace(download_path=str(tmp_path), max_download_bytes=limit)

    result = await cli._download_operation(spec, client, args, 17, cfg)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v2/sqlQueries/executeOntology"
    assert request.headers["accept"] == "application/octet-stream"
    assert json.loads(request.content) == expected
    assert request.extensions["timeout"]["read"] == 17
    assert Path(result["file_path"]).read_bytes() == payload[:limit]
    assert result["file_size"] == min(len(payload), limit)
    assert result["truncated"] is (len(payload) > limit)


@pytest.mark.asyncio
async def test_get_results_keeps_scalar_id_and_local_output(sql_transport, tmp_path):
    client, requests, payload = sql_transport
    args = cli.build_parser().parse_args(
        ["query", "get-results", "query-123", "--output", "rows.arrow"]
    )
    spec = cli._spec_for("sql_query", "get_results")
    cli._validate_inputs(spec, args)
    cfg = SimpleNamespace(download_path=str(tmp_path), max_download_bytes=100)

    result = await cli._download_operation(spec, client, args, 17, cfg)

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == "/api/v2/sqlQueries/query-123/getResults"
    assert requests[0].content == b""
    assert requests[0].url.query == b""
    output = Path(result["file_path"])
    assert output.name == "rows.arrow"
    assert output.read_bytes() == payload
