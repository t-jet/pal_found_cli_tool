"""Exercise Functions streaming through the SDK's HTTP and SSE decoder."""

import asyncio
import json
import sys
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import MagicMock

import httpx
import pytest
from foundry_sdk import AsyncFoundryClient, UserTokenAuth
from foundry_sdk._core.config import Config

from pal_found_cli.functions.scripts import pal_found_functions_cli as cli


DATA = {"type": "data", "value": [{"name": "caf\u00e9", "count": 2}]}
ERROR = {
    "type": "error",
    "errorCode": "INVALID_ARGUMENT",
    "errorName": "QueryRuntimeError",
    "errorInstanceId": "3f8a9c7b-2e4d-4a1f-9b8c-7d6e5f4a3b2c",
    "errorDescription": "Partial failure",
    "parameters": {},
}


def _event(payload):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, error=None, block=False):
        self.chunks = chunks
        self.error = error
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture
def sdk_stream(monkeypatch):
    class Cfg:
        timeout_s = 30
        log_level = "INFO"

        def load(self):
            pass

    monkeypatch.setattr(cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(cli, "LogSetup", MagicMock())
    monkeypatch.setattr(cli, "AccessControlGuard", lambda *args: MagicMock())
    monkeypatch.setenv("FOUNDRY_INCLUDE_TRACEBACK", "false")
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_RETRY_INITIAL_DELAY_MS", "0")
    monkeypatch.setattr(sys, "argv", [
        "prog", "query", "streaming-execute", "orders", "--parameters", '{"x":2}',
        "--format", "toon", "--pretty",
    ])

    @asynccontextmanager
    async def connect(streams, *, status=200, content_type="text/event-stream"):
        requests = []

        class Factory:
            active = False
            exited = False

            @contextmanager
            def invocation_scope(self, cfg):
                self.active = True
                try:
                    yield
                finally:
                    self.active = False
                    self.exited = True

            def create(self, cfg):
                return sdk

        factory = Factory()

        def respond(request):
            assert factory.active
            requests.append(request)
            return httpx.Response(status, headers={"content-type": content_type},
                                  stream=streams[len(requests) - 1])

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            monkeypatch.setattr("foundry_sdk._core.api_client.AsyncHttpClient", lambda *args, **kwargs: transport)
            sdk = AsyncFoundryClient(auth=UserTokenAuth("offline"), hostname="https://offline.invalid",
                                     config=Config(max_retries=0), preview=True)
            monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
            yield factory, requests

    return connect


@pytest.mark.asyncio
async def test_real_sdk_sse_data_and_error_become_ndjson(sdk_stream, capsys):
    # Multiline SSE data, heartbeat, framing fields, and split UTF-8 bytes all
    # pass through the installed SDK decoder before reaching the CLI.
    wire = (b': heartbeat\n\nid: batch-1\nevent: batch\nretry: 100\n'
            b'data: {"type":"data",\ndata: "value":[{"name":"caf\xc3\xa9","count":2}]}\n\n'
            + _event(ERROR))
    split = wire.index(b"\xc3") + 1
    stream = _Stream([wire[:split], wire[split:split + 1], wire[split + 1:]])
    async with sdk_stream([stream]) as (factory, requests):
        assert await cli.main() == cli.EXIT_SUCCESS
        output = capsys.readouterr().out
        assert [json.loads(line) for line in output.splitlines()] == [DATA, ERROR]
        assert output.endswith("\n")
        assert "data: " not in output
        assert requests[0].headers["accept"] == "text/event-stream"
        assert json.loads(requests[0].content)["parameters"] == {"x": 2}
        assert stream.closed and factory.exited


@pytest.mark.asyncio
async def test_empty_sse_stream_is_empty_success(sdk_stream, capsys):
    stream = _Stream([b": heartbeat\n\nevent: ping\n\n"])
    async with sdk_stream([stream]) as (factory, requests):
        assert await cli.main() == cli.EXIT_SUCCESS
        assert capsys.readouterr().out == ""
        assert len(requests) == 1
        assert stream.closed and factory.exited


@pytest.mark.asyncio
async def test_stream_timeout_retry_discards_failed_attempt(sdk_stream, capsys):
    failed = _Stream([_event(DATA)], error=TimeoutError("stream timed out"))
    complete = _Stream([_event(DATA), _event(ERROR)])
    async with sdk_stream([failed, complete]) as (factory, requests):
        assert await cli.main() == cli.EXIT_SUCCESS
        assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [DATA, ERROR]
        assert len(requests) == 2
        assert failed.closed and complete.closed and factory.exited


@pytest.mark.asyncio
async def test_stream_read_failure_uses_main_error_boundary(sdk_stream, capsys):
    stream = _Stream([_event(DATA)], error=httpx.ReadError("connection lost"))
    async with sdk_stream([stream]) as (factory, requests):
        assert await cli.main() == cli.EXIT_SERVER_ERROR
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["error"] is True
        assert envelope["exception_type"] == "ReadError"
        assert len(requests) == 1
        assert stream.closed and factory.exited


@pytest.mark.asyncio
@pytest.mark.parametrize("wire,content_type", [
    (b"data: not-json\n\n", "text/event-stream"),
    (b'data: {"type":"data"}\n\n', "text/event-stream"),
    (b"not an event stream", "application/json"),
])
async def test_invalid_sse_returns_structured_error_without_partial_output(
    sdk_stream, capsys, wire, content_type,
):
    stream = _Stream([_event(DATA), wire])
    async with sdk_stream([stream], content_type=content_type) as (factory, requests):
        assert await cli.main() == cli.EXIT_SERVER_ERROR
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["error"] is True
        assert len(requests) == 1
        assert stream.closed and factory.exited


@pytest.mark.asyncio
async def test_stream_timeout_covers_iteration_and_discards_partial_output(
    sdk_stream, monkeypatch, capsys,
):
    # Keep the real retry implementation, using a short per-attempt deadline.
    retry_handler = cli.RetryHandler
    monkeypatch.setattr(cli, "RetryHandler", lambda **kwargs: retry_handler(timeout_s=0.02, max_attempts=2))
    streams = [_Stream([_event(DATA)], block=True) for _ in range(2)]
    async with sdk_stream(streams) as (factory, requests):
        assert await cli.main() == cli.EXIT_TIMEOUT
        assert json.loads(capsys.readouterr().out)["exit_code"] == cli.EXIT_TIMEOUT
        assert len(requests) == 2
        assert all(stream.closed for stream in streams) and factory.exited


@pytest.mark.asyncio
async def test_stream_cancellation_closes_response_and_leaves_stdout_empty(sdk_stream, capsys):
    stream = _Stream([_event(DATA)], block=True)
    async with sdk_stream([stream]) as (factory, requests):
        task = asyncio.create_task(cli.main())
        try:
            await asyncio.wait_for(stream.waiting.wait(), timeout=2)
            assert capsys.readouterr().out == ""
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert capsys.readouterr().out == ""
        assert len(requests) == 1
        assert stream.closed and factory.exited
