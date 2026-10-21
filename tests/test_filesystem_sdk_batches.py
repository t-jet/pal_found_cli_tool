"""Filesystem batch requests through the installed SDK and an offline transport."""

import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from foundry_sdk import UserTokenAuth, _core
from foundry_sdk.v2.filesystem import models
from foundry_sdk.v2.filesystem.folder import AsyncFolderClient
from foundry_sdk.v2.filesystem.resource import AsyncResourceClient
from pydantic import ValidationError

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from pal_found_cli.filesystem.scripts import pal_found_filesystem_cli as cli


RID = "ri.compass.main.folder.01a79a9d-e293-48db-a585-9ffe221536e8"
BATCHES = [
    ("folder", "get_batch", "folderRid", models.GetFoldersBatchResponse),
    ("resource", "get_batch", "resourceRid", models.GetResourcesBatchResponse),
    ("resource", "get_by_path_batch", "path", models.GetByPathResourcesBatchResponse),
]


@pytest.fixture
async def sdk_clients(monkeypatch):
    requests = []
    record = {
        "rid": RID,
        "displayName": "Example folder",
        "path": "/Example folder",
        "type": "FOLDER",
        "createdBy": "00000000-0000-0000-0000-000000000001",
        "updatedBy": "00000000-0000-0000-0000-000000000001",
        "createdTime": "2026-01-01T00:00:00Z",
        "updatedTime": "2026-01-01T00:00:00Z",
        "trashStatus": "NOT_TRASHED",
        "parentFolderRid": RID,
        "projectRid": "ri.compass.main.project.example",
        "spaceRid": "ri.compass.main.space.example",
    }

    def respond(request):
        requests.append(request)
        data: list[dict[str, str]] | dict[str, dict[str, str]]
        if request.url.path.endswith("getByPathBatch"):
            data = [record]
        else:
            data = {RID: record}
        return httpx.Response(200, json={"data": data})

    clients: dict[str, AsyncFolderClient | AsyncResourceClient] = {
        "folder": AsyncFolderClient(
            auth=UserTokenAuth("offline"), hostname="offline.invalid",
            config=_core.Config(max_retries=0),
        ),
        "resource": AsyncResourceClient(
            auth=UserTokenAuth("offline"), hostname="offline.invalid",
            config=_core.Config(max_retries=0),
        ),
    }
    for client in clients.values():
        await client._api_client._client.aclose()
        monkeypatch.setattr(client._api_client, "_client", httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ))
    try:
        yield clients, requests
    finally:
        for client in clients.values():
            await client._api_client._client.aclose()


@pytest.mark.parametrize("resource,operation,key,response_type", BATCHES)
async def test_positional_json_batch_serializes_and_decodes_sdk_response(
    sdk_clients, resource, operation, key, response_type
):
    clients, requests = sdk_clients
    body = [{key: "/Example folder" if key == "path" else RID}]
    args = cli.build_parser().parse_args(
        [resource, operation.replace("_", "-"), json.dumps(body)]
    )

    result = await cli._invoke(resource, operation, clients[resource], args, 5)

    assert isinstance(result, response_type)
    records = result.data if isinstance(result.data, list) else list(result.data.values())
    assert len(records) == 1
    assert isinstance(records[0], models.Folder if resource == "folder" else models.Resource)
    assert records[0].rid == RID
    assert records[0].display_name == "Example folder"
    assert len(requests) == 1
    assert requests[0].method == "POST"
    endpoint = "getByPathBatch" if operation == "get_by_path_batch" else "getBatch"
    collection = "folders" if resource == "folder" else "resources"
    assert requests[0].url.path == f"/api/v2/filesystem/{collection}/{endpoint}"
    assert requests[0].headers["content-type"] == "application/json"
    assert json.loads(requests[0].content) == body


@pytest.mark.parametrize("resource,operation,key,response_type", BATCHES)
@pytest.mark.parametrize("body", ["[]", "{}", '[{"unexpected":"value"}]'])
async def test_batch_keeps_sdk_input_validation(
    sdk_clients, resource, operation, key, response_type, body
):
    clients, requests = sdk_clients
    args = cli.build_parser().parse_args([resource, operation.replace("_", "-"), body])

    with pytest.raises(ValidationError):
        await cli._invoke(resource, operation, clients[resource], args, 5)

    assert requests == []


@pytest.mark.parametrize("resource,operation,key,response_type", BATCHES)
async def test_malformed_batch_json_returns_input_error_without_http_or_payload(
    sdk_clients, monkeypatch, capsys, resource, operation, key, response_type
):
    clients, requests = sdk_clients
    cfg = SimpleNamespace(timeout_s=30, log_level="INFO", load=lambda: None)

    @contextmanager
    def scope(config):
        yield

    sdk = SimpleNamespace(filesystem=SimpleNamespace(
        Folder=clients["folder"], Resource=clients["resource"]
    ))
    factory = SimpleNamespace(invocation_scope=scope, create=lambda config: sdk)
    monkeypatch.setattr(cli, "ConfigLoader", lambda: cfg)
    monkeypatch.setattr(cli, "LogSetup", MagicMock())
    monkeypatch.setattr(cli, "AccessControlGuard", lambda config, namespace: MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(sys, "argv", [
        "prog", resource, operation.replace("_", "-"), '["private-batch-value"',
    ])
    error_logger = logging.getLogger("pal_found_cli.common.error_serializer")
    handler = logging.StreamHandler(sys.stderr)
    error_logger.addHandler(handler)
    try:
        code = await cli.main()
    finally:
        error_logger.removeHandler(handler)

    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert code == cli.EXIT_USER_INPUT == 1
    assert envelope["exit_code"] == 1
    assert envelope["exception_type"] == "JSONDecodeError"
    assert "JSONDecodeError" in captured.err
    assert "private-batch-value" not in captured.out + captured.err
    assert requests == []


@pytest.mark.parametrize("resource", ["folder", "project", "resource", "space"])
async def test_non_json_positional_identifiers_remain_strings(resource):
    args = cli.build_parser().parse_args([resource, "get", "123"])
    client = MagicMock()
    client.get.return_value = {"rid": "123"}

    result = await cli._invoke(resource, "get", client, args, 5)

    client.get.assert_called_once_with("123", request_timeout=5)
    assert result == {"rid": "123"}
