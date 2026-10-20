"""Build search keeps its required filter across real SDK continuation requests."""

import json
from unittest.mock import patch

import httpx
import pytest
from foundry_sdk import AsyncFoundryClient, UserTokenAuth
from foundry_sdk._core.config import Config

from pal_found_cli.orchestration.scripts import pal_found_orchestration_cli as cli


@pytest.mark.asyncio
async def test_build_search_preserves_required_filter_on_every_page():
    where = {"type": "eq", "field": "STATUS", "value": "RUNNING"}
    args = cli.build_parser().parse_args([
        "build", "search", "--where-json", json.dumps(where), "--max-pages", "2",
    ])
    spec = cli._spec_for("build", "search")
    cli._validate_inputs(spec, args)
    requests = []

    def page(request):
        requests.append(request)
        return httpx.Response(200, json={
            "data": [], **({"nextPageToken": "next-build-page"} if len(requests) == 1 else {}),
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(page)) as transport:
        with patch("foundry_sdk._core.api_client.AsyncHttpClient", return_value=transport):
            root = AsyncFoundryClient(
                auth=UserTokenAuth("offline"), hostname="https://offline.invalid",
                config=Config(max_retries=0), preview=True,
            )
            records, _ = await cli._paginate_operation(spec, root.orchestration.Build, args, 30)

    assert records == []
    assert len(requests) == 2
    for request in requests:
        assert request.method == "POST"
        assert request.url.path == "/api/v2/orchestration/builds/search"
        assert json.loads(request.content)["where"] == where
    assert json.loads(requests[1].content)["pageToken"] == "next-build-page"
