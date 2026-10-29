"""Model Studio config-version GET through the real SDK and an offline wire."""

from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from foundry_sdk import UserTokenAuth, _core
from foundry_sdk.v2.models.model_studio_config_version import (
    AsyncModelStudioConfigVersionClient,
)

from pal_found_cli.common.access_control_guard import AccessControlError
from pal_found_cli.models.scripts import pal_found_models_cli as cli


RID = "ri.models.main.model-studio.00000000-0000-0000-0000-000000000001"
RECORD = {
    "name": "offline-config",
    "version": 1,
    "trainerId": "ri.models.main.model-studio-trainer.00000000-0000-0000-0000-000000000002",
    "trainer": {
        "trainerId": "ri.models.main.model-studio-trainer.00000000-0000-0000-0000-000000000002",
        "version": "1",
    },
    "workerConfig": {"inputs": {}, "outputs": {}},
    "resources": {"memory": "4G", "cpu": "2"},
    "createdBy": "00000000-0000-0000-0000-000000000003",
    "createdTime": "2026-01-01T00:00:00Z",
}


@pytest.fixture
async def wire(monkeypatch: pytest.MonkeyPatch):
    """Route a real SDK resource through one denied-network fake transport."""
    requests: list[httpx.Request] = []
    responses: list[httpx.Response] = []
    status = {"value": 200, "failures": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status["failures"]:
            status["failures"] -= 1
            response = httpx.Response(
                503,
                json={
                    "errorName": "OfflineUnavailable",
                    "errorInstanceId": "offline",
                    "parameters": {},
                },
            )
        elif status["value"] == 200:
            response = httpx.Response(200, json=RECORD)
        else:
            response = httpx.Response(
                status["value"],
                json={
                    "errorName": (
                        "ModelStudioConfigVersionNotFound"
                        if status["value"] == 404
                        else "OfflinePermissionDenied"
                    ),
                    "errorInstanceId": "offline",
                    "parameters": {"studioRid": RID, "configVersion": 1},
                },
            )
        responses.append(response)
        return response

    resource = AsyncModelStudioConfigVersionClient(
        auth=UserTokenAuth("offline-token"),
        hostname="offline.invalid",
        config=_core.Config(max_retries=0),
    )
    await resource._api_client._client.aclose()
    resource._api_client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond)
    )

    @contextmanager
    def scope(*args: Any, **kwargs: Any):
        yield

    class Factory:
        def invocation_scope(self, *args: Any, **kwargs: Any):
            return scope()

        def create(self, *args: Any, **kwargs: Any):
            return SimpleNamespace(
                models=SimpleNamespace(
                    ModelStudio=SimpleNamespace(ConfigVersion=resource)
                )
            )

    class Config:
        timeout_s = 30
        log_level = "ERROR"

        def load(self) -> None:
            pass

    monkeypatch.setattr(cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(cli, "ConfigLoader", Config)
    monkeypatch.setattr(cli.LogSetup, "configure", lambda **kwargs: None)
    monkeypatch.setattr(
        cli,
        "AccessControlGuard",
        lambda *args, **kwargs: SimpleNamespace(check=lambda *args: None),
    )
    try:
        yield requests, responses, status, resource
    finally:
        await resource._api_client._client.aclose()


@pytest.mark.asyncio
async def test_real_sdk_get_serializes_integer_path_and_typed_result(
    wire: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests, responses, _, resource = wire
    original_api_client = resource._api_client
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pal-found-models",
            "model-studio-config-version",
            "get",
            RID,
            "1",
            "--format",
            "json",
            "--timeout",
            "7",
        ],
    )

    assert await cli.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["version"] == 1
    assert output["name"] == "offline-config"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == f"/api/v2/models/modelStudios/{RID}/configVersions/1"
    assert requests[0].content == b""
    assert requests[0].extensions["timeout"]["read"] == 7
    assert len(responses) == 1 and responses[0].is_closed
    assert resource._api_client is original_api_client


@pytest.mark.asyncio
async def test_sdk_rejects_noninteger_version_before_transport(
    wire: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests, _, _, resource = wire
    original_api_client = resource._api_client
    monkeypatch.setattr(
        sys,
        "argv",
        ["pal-found-models", "model-studio-config-version", "get", RID, "v1"],
    )

    assert await cli.main() == cli.EXIT_USER_INPUT

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["error"] is True
    assert requests == []
    assert resource._api_client is original_api_client


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [403, 404])
async def test_real_sdk_errors_keep_envelope_and_cleanup(
    wire: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status_code: int,
) -> None:
    requests, responses, status, resource = wire
    status["value"] = status_code
    original_api_client = resource._api_client
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pal-found-models",
            "model-studio-config-version",
            "get",
            RID,
            "1",
            "--format",
            "json",
        ],
    )

    assert await cli.main() != 0

    envelope = json.loads(capsys.readouterr().out)
    assert envelope["error"] is True
    assert envelope["http_status"] == status_code
    assert "offline-token" not in json.dumps(envelope)
    assert len(requests) == 1
    assert all(response.is_closed for response in responses)
    assert resource._api_client is original_api_client


@pytest.mark.asyncio
async def test_guard_denial_prevents_real_sdk_transport(
    wire: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests, _, _, _ = wire

    class DenyGuard:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def check(self, *args: Any) -> None:
            raise AccessControlError("offline denial")

    monkeypatch.setattr(cli, "AccessControlGuard", DenyGuard)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pal-found-models", "model-studio-config-version", "get", RID, "1"],
    )

    assert await cli.main() == cli.EXIT_ACCESS_CONTROL
    assert requests == []
    assert "offline-token" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_default_four_attempts_keep_same_route_and_restore_client(
    wire: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests, responses, status, resource = wire
    status["failures"] = 3
    original_api_client = resource._api_client
    monkeypatch.delenv("FOUNDRY_AGENTIC_CLI_RETRY_MAX_ATTEMPTS", raising=False)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_INITIAL_DELAY_MS", "0")
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_JITTER", "false")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pal-found-models",
            "model-studio-config-version",
            "get",
            RID,
            "1",
            "--format",
            "json",
        ],
    )

    assert await cli.main() == 0

    assert json.loads(capsys.readouterr().out)["version"] == 1
    assert len(requests) == 4
    assert {request.url.path for request in requests} == {
        f"/api/v2/models/modelStudios/{RID}/configVersions/1"
    }
    assert all(response.is_closed for response in responses)
    assert resource._api_client is original_api_client
