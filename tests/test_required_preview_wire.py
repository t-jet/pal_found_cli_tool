"""Required preview selectors on real SDK requests, including continuation pages."""

import importlib
import json
from unittest.mock import patch

import httpx
import pytest
from foundry_sdk import AsyncFoundryClient, UserTokenAuth
from foundry_sdk._core.config import Config

RECORD = "ri.checkpoints.main.record.offline"
JOB = "ri.scheduler.main.job.offline"
VERSION = "ri.scheduler.main.schedule-version.offline"
FILTER = '{"filter":{"type":"and","filters":[]}}'
CASES = [
    ("checkpoints", ["record", "get", RECORD], "GET", f"/checkpoints/records/{RECORD}"),
    ("checkpoints", ["record", "get-batch", "--records-json", json.dumps([{"recordRid": RECORD}])],
     "POST", "/checkpoints/records/getBatch"),
    ("checkpoints", ["record", "search", "--where-json", FILTER], "POST", "/checkpoints/records/search"),
    ("orchestration", ["job", "get", JOB], "GET", f"/orchestration/jobs/{JOB}"),
    ("orchestration", ["job", "get-batch", "--job-rids-json", json.dumps([JOB])],
     "POST", "/orchestration/jobs/getBatch"),
    ("orchestration", ["schedule-version", "get", VERSION], "GET", f"/orchestration/scheduleVersions/{VERSION}"),
    ("orchestration", ["schedule-version", "schedule", VERSION], "GET", f"/orchestration/scheduleVersions/{VERSION}/schedule"),
]


class RequestCaptured(Exception):
    """Stop after serializing a request without asserting a server response."""


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace,argv,method,path", CASES)
async def test_required_preview_is_serialized_as_query(namespace, argv, method, path):
    cli = importlib.import_module(f"pal_found_cli.{namespace}.scripts.pal_found_{namespace}_cli")
    args = cli.build_parser().parse_args(argv)
    spec = cli._spec_for(args.resource.replace("-", "_"), args.operation.replace("-", "_"))
    cli._validate_inputs(spec, args)
    requests = []

    def capture(request):
        requests.append(request)
        raise RequestCaptured

    async with httpx.AsyncClient(transport=httpx.MockTransport(capture)) as transport:
        with patch("foundry_sdk._core.api_client.AsyncHttpClient", return_value=transport):
            root = AsyncFoundryClient(auth=UserTokenAuth("offline"), hostname="https://offline.invalid",
                                     config=Config(max_retries=0), preview=True)
            client = cli._get_client(root, spec["client_path"])
            with pytest.raises(RequestCaptured):
                await cli._invoke(spec, client, args, 30)
    assert len(requests) == 1
    request = requests[0]
    assert (request.method, request.url.path) == (method, "/api/v2" + path)
    assert request.url.params.get_list("preview") == ["true"]
    if request.content:
        assert "preview" not in json.loads(request.content)


@pytest.mark.asyncio
async def test_checkpoint_search_preserves_preview_on_continuation():
    from pal_found_cli.checkpoints.scripts import pal_found_checkpoints_cli as cli

    args = cli.build_parser().parse_args(["record", "search", "--where-json", FILTER, "--max-pages", "2"])
    spec = cli._spec_for("record", "search")
    cli._validate_inputs(spec, args)
    requests = []

    def page(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [], **({"nextPageToken": "next"} if len(requests) == 1 else {})})

    async with httpx.AsyncClient(transport=httpx.MockTransport(page)) as transport:
        with patch("foundry_sdk._core.api_client.AsyncHttpClient", return_value=transport):
            root = AsyncFoundryClient(auth=UserTokenAuth("offline"), hostname="https://offline.invalid",
                                     config=Config(max_retries=0), preview=True)
            records, _ = await cli._paginate_operation(spec, root.checkpoints.Record, args, 30)
    assert records == []
    assert len(requests) == 2
    assert all(request.url.params.get_list("preview") == ["true"] for request in requests)
    assert json.loads(requests[1].content)["pageToken"] == "next"
