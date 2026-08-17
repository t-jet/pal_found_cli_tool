"""Unit tests for Foundry AIP Agents CLI contracts."""

from __future__ import annotations

import argparse
import contextvars
import json
import sys
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from pal_found_cli.aip_agents.scripts import pal_found_aip_agents_cli as cli
from pal_found_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from pal_found_cli.common import async_client_factory as factory_module
from pal_found_cli.common.async_client_factory import AsyncClientFactory
from pal_found_cli.common.error_serializer import ErrorSerializer
from pal_found_cli.common.retry import RetryHandler
from pal_found_cli.common.sdk_error_utils import sdk_http_status
from pal_found_cli.common.session_manager import SessionManager, SessionState
from foundry_sdk._errors import (
    ApiNotFoundError,
    BadRequestError,
    ConflictError,
    ConnectionError as SDKConnectionError,
    EnvironmentNotConfigured,
    NotFoundError,
    NotAuthenticated,
    PermissionDeniedError,
    RateLimitError,
    SDKInternalError,
    ServiceUnavailable,
    TimeoutError as SDKTimeoutError,
    UnauthorizedError,
)


class _RawResponse:
    def __init__(self, items: list[Any], token: str | None) -> None:
        self.page = SimpleNamespace(data=items, next_page_token=token)

    def decode(self) -> Any:
        return self.page


class _Scope(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: Any) -> Literal[False]:
        return False


class _Factory:
    def __init__(self, root: Any) -> None:
        self.root = root
        self.scope_kwargs: dict[str, Any] = {}
        self.create_kwargs: dict[str, Any] = {}

    def invocation_scope(self, cfg: Any, **kwargs: Any) -> _Scope:
        self.scope_kwargs = kwargs
        return _Scope()

    def create(self, cfg: Any, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        return self.root


class _ImmediateRetry:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def execute(self, function: Any, *args: Any, **kwargs: Any) -> Any:
        return await function(*args, **kwargs)


class _Cfg:
    def __init__(self, root: Path) -> None:
        self.session_path = root / "sessions"
        self.download_path = root / "downloads"
        self.max_download_bytes = 4
        self.timeout_s = 30
        self.log_level = "ERROR"
        self.global_readonly = False
        self.global_metadata_only = False

    def load(self) -> None:
        return None


def _root() -> tuple[Any, Any, Any, Any, Any]:
    agent = SimpleNamespace()
    versions = SimpleNamespace()
    session = SimpleNamespace()
    content = SimpleNamespace()
    trace = SimpleNamespace()
    agent.AgentVersion = versions
    agent.Session = session
    session.Content = content
    session.SessionTrace = trace
    return SimpleNamespace(aip_agents=SimpleNamespace(Agent=agent)), agent, versions, session, content


def _patch_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, factory: _Factory) -> _Cfg:
    cfg = _Cfg(tmp_path)
    monkeypatch.setattr(cli, "ConfigLoader", lambda: cfg)
    monkeypatch.setattr(cli.LogSetup, "configure", MagicMock())
    monkeypatch.setattr(cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(cli, "RetryHandler", _ImmediateRetry)
    return cfg


def test_catalog_has_exact_15_unique_sdk_operations() -> None:
    keys = {(spec["resource"], spec["operation"]) for spec in cli.OP_SPECS}
    assert len(cli.OP_SPECS) == 15
    assert len(keys) == 15
    assert ("session", "purge") not in keys


def test_catalog_routes_match_sdk_v2_contract() -> None:
    routes = {
        (spec["resource"], spec["operation"]): (
            ".".join(spec["client_path"]),
            spec["method"],
        )
        for spec in cli.OP_SPECS
    }
    assert routes == {
        ("agent", "all_sessions"): ("Agent", "all_sessions"),
        ("agent", "get"): ("Agent", "get"),
        ("agent_version", "get"): ("Agent.AgentVersion", "get"),
        ("agent_version", "list"): ("Agent.AgentVersion", "list"),
        ("session", "blocking_continue"): ("Agent.Session", "blocking_continue"),
        ("session", "cancel"): ("Agent.Session", "cancel"),
        ("session", "create"): ("Agent.Session", "create"),
        ("session", "delete"): ("Agent.Session", "delete"),
        ("session", "get"): ("Agent.Session", "get"),
        ("session", "list"): ("Agent.Session", "list"),
        ("session", "rag_context"): ("Agent.Session", "rag_context"),
        ("session", "streaming_continue"): ("Agent.Session", "streaming_continue"),
        ("session", "update_title"): ("Agent.Session", "update_title"),
        ("content", "get"): ("Agent.Session.Content", "get"),
        ("session_trace", "get"): ("Agent.Session.SessionTrace", "get"),
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["agent", "all-sessions"],
        ["agent", "get", "agent-rid"],
        ["agent-version", "get", "agent-rid", "1.0"],
        ["agent-version", "list", "agent-rid"],
        ["session", "blocking-continue", "--alias", "a", "--parameter-inputs-json", "{}", "--user-input-json", "{}"],
        ["session", "cancel", "--alias", "a", "--message-id", "m"],
        ["session", "create", "--alias", "a", "--agent-rid", "r"],
        ["session", "delete", "--alias", "a"],
        ["session", "get", "--alias", "a"],
        ["session", "list", "agent-rid"],
        ["session", "rag-context", "--alias", "a", "--parameter-inputs-json", "{}", "--user-input-json", "{}"],
        ["session", "streaming-continue", "--alias", "a", "--parameter-inputs-json", "{}", "--user-input-json", "{}"],
        ["session", "update-title", "--alias", "a", "--title", "t"],
        ["session", "purge"],
        ["content", "get", "--alias", "a"],
        ["session-trace", "get", "--alias", "a", "--session-trace-id", "t"],
    ],
)
def test_parser_accepts_every_command(argv: list[str]) -> None:
    parsed = cli.build_parser().parse_args(argv)
    assert parsed.resource
    assert parsed.operation


def test_json_validators_enforce_top_level_shapes() -> None:
    assert cli._parse_json_object('{"a": 1}', field="x") == {"a": 1}
    assert cli._parse_json_list('[{"a": 1}]', field="x") == [{"a": 1}]
    with pytest.raises(ValueError, match="JSON object"):
        cli._parse_json_object("[]", field="x")
    with pytest.raises(ValueError, match="array of objects"):
        cli._parse_json_list("[1]", field="x")
    with pytest.raises(ValueError, match="valid JSON"):
        cli._parse_json_object("{", field="x")


def test_cancel_response_is_forwarded_as_scalar() -> None:
    spec = cli._spec_for("session", "cancel")
    args = argparse.Namespace(message_id="m", response="**stop**")
    state = SessionState("s", "a", None, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "active", [])
    positional, kwargs = cli._sdk_call_parts(spec, args, state)
    assert positional == ["a", "s"]
    assert kwargs == {"message_id": "m", "response": "**stop**"}


def test_nested_client_resolution_uses_exact_public_route() -> None:
    root, _, _, session, _ = _root()
    assert cli._get_client(root, ("Agent", "Session", "SessionTrace")) is session.SessionTrace


@pytest.mark.asyncio
async def test_raw_pagination_fetches_exact_pages_and_cursor() -> None:
    method = AsyncMock(
        side_effect=[_RawResponse([1, 2], "next"), _RawResponse([3], None)]
    )
    args = argparse.Namespace(page_size=2, page_token="start", batch_pages=2)
    items, helper = await cli._paginate_operation(method, args, 9, ("agent",))
    assert items == [1, 2, 3]
    assert helper.pages_fetched == 2
    assert helper.next_page_token is None
    assert method.await_args_list[0].args == ("agent",)
    assert method.await_args_list[0].kwargs["page_token"] == "start"
    assert method.await_args_list[1].kwargs["page_token"] == "next"


@pytest.mark.asyncio
async def test_create_then_alias_get_updates_sanitized_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, _, session, _ = _root()
    session.create = AsyncMock(return_value=SimpleNamespace(rid="session-rid"))
    session.delete = AsyncMock(return_value=None)
    factory = _Factory(root)
    cfg = _patch_main(monkeypatch, tmp_path, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "session", "create", "--alias", " Demo Alias ", "--agent-rid", "agent-rid"])
    assert await cli.main() == 0
    created = json.loads(capsys.readouterr().out)
    assert created["alias"] == "demo-alias"
    assert "session_token" not in created
    session.get = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"rid": "session-rid"}))
    monkeypatch.setattr(sys, "argv", ["cmd", "session", "get", "--alias", "demo-alias", "--format", "json"])
    assert await cli.main() == 0
    json.loads(capsys.readouterr().out)
    state = SessionManager(config=cfg).load("demo-alias")
    assert state.tool_history[-1] == {
        "timestamp": state.last_used_at,
        "operation": "session.get",
        "succeeded": True,
    }
    assert factory.scope_kwargs == {"include_attribution": False}
    assert factory.create_kwargs == {"include_attribution": False}


@pytest.mark.asyncio
async def test_streaming_continue_persists_only_configured_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, _, _, session, _ = _root()
    factory = _Factory(root)
    cfg = _patch_main(monkeypatch, tmp_path, factory)
    manager = SessionManager(config=cfg)
    now = datetime.now(UTC).isoformat()
    state = SessionState("s", "a", None, now, now, "active", [])
    manager.update("alias", state)
    session.streaming_continue = AsyncMock(return_value=b"abcdef")
    monkeypatch.setattr(sys, "argv", ["cmd", "session", "streaming-continue", "--alias", "alias", "--parameter-inputs-json", "{}", "--user-input-json", "{}", "--format", "toon"])
    assert await cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["file_size"] == 4
    assert result["truncated"] is True
    assert Path(result["file_path"]).read_bytes() == b"abcd"


@pytest.mark.asyncio
async def test_purge_is_local_idempotent_and_makes_no_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, *_ = _root()
    factory = _Factory(root)
    cfg = _patch_main(monkeypatch, tmp_path, factory)
    manager = SessionManager(config=cfg)
    now = datetime.now(UTC).isoformat()
    state = SessionState("s", "a", None, now, now, "active", [])
    manager.update("alias", state)
    monkeypatch.setattr(sys, "argv", ["cmd", "session", "purge"])
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"purged_sessions": 1}
    assert factory.create_kwargs == {}
    assert await cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"purged_sessions": 0}


@pytest.mark.asyncio
async def test_purge_acl_denial_precedes_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, *_ = _root()
    factory = _Factory(root)
    cfg = _patch_main(monkeypatch, tmp_path, factory)
    manager = SessionManager(config=cfg)
    now = datetime.now(UTC).isoformat()
    state = SessionState("s", "a", None, now, now, "active", [])
    manager.update("alias", state)
    monkeypatch.setenv("FOUNDRY_AGENTIC_CLI_READONLY", "true")
    monkeypatch.setattr(sys, "argv", ["cmd", "session", "purge"])
    assert await cli.main() == 8
    assert json.loads(capsys.readouterr().out)["exit_code"] == 8
    assert manager.load("alias").session_id == "s"


def test_metadata_policy_is_exact_six_permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(global_readonly=False, global_metadata_only=True)
    guard = AccessControlGuard(cfg, "AIP_AGENTS", str(cli._METADATA_ALLOWLIST_PATH))
    permitted = 0
    for spec in cli.OP_SPECS:
        try:
            guard.check(spec["resource"], spec["operation"])
            permitted += 1
        except AccessControlError:
            pass
    assert permitted == 6


def test_attribution_opt_out_restores_nested_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attribution = contextvars.ContextVar("test_attribution", default=None)
    original = attribution.set(["outer"])
    monkeypatch.setitem(sys.modules, "foundry_sdk", SimpleNamespace(ATTRIBUTION_VAR=attribution))

    class Tracing:
        def __init__(self, config: Any) -> None:
            pass

        def scope(self, supplied: Any) -> _Scope:
            return _Scope()

    monkeypatch.setattr(factory_module, "TracingProvider", Tracing)
    cfg = SimpleNamespace(enable_attribution=True, attribution_rids="configured")
    factory = AsyncClientFactory()
    with factory.invocation_scope(cfg, include_attribution=False):
        assert attribution.get() is None
        with factory.invocation_scope(cfg, include_attribution=True):
            assert attribution.get() == ["configured"]
        assert attribution.get() is None
    assert attribution.get() == ["outer"]
    attribution.reset(original)


def test_create_opt_out_does_not_overwrite_scoped_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attribution = contextvars.ContextVar("test_create_attribution", default=None)
    client = object()
    sdk = SimpleNamespace(
        ATTRIBUTION_VAR=attribution,
        AsyncFoundryClient=MagicMock(return_value=client),
        UserTokenAuth=MagicMock(return_value=object()),
    )
    monkeypatch.setitem(sys.modules, "foundry_sdk", sdk)
    cfg = SimpleNamespace(
        token="token",
        hostname="host",
        enable_attribution=True,
        attribution_rids="configured",
    )
    token = attribution.set(["outer"])
    assert AsyncClientFactory().create(cfg, include_attribution=False) is client
    assert attribution.get() is None
    attribution.reset(token)


def test_unexpected_error_does_not_expose_exception_details(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    secret = "prompt-and-session-token"
    assert cli._serialize_error(RuntimeError(secret)) == 6
    output = capsys.readouterr().out
    assert secret not in output
    assert secret not in caplog.text
    assert json.loads(output)["message"] == "AIP Agents operation failed"


@pytest.mark.parametrize(
    ("exception", "status", "exit_code"),
    [
        (UnauthorizedError({}), 401, 2),
        (PermissionDeniedError({}), 403, 3),
        (NotFoundError({}), 404, 4),
        (RateLimitError("rate limited", "test"), 429, 7),
        (ServiceUnavailable("unavailable", "test"), 503, 6),
        (ApiNotFoundError("missing API"), None, 4),
        (BadRequestError({}), None, 1),
        (ConflictError({}), None, 1),
        (NotAuthenticated(), None, 2),
        (EnvironmentNotConfigured("missing environment"), None, 9),
        (SDKTimeoutError(), None, 5),
        (SDKConnectionError(), None, 6),
        (SDKInternalError("internal"), None, 6),
    ],
)
def test_actual_sdk_errors_map_to_http_and_adr_codes(
    exception: Exception,
    status: int | None,
    exit_code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sdk_http_status(exception) == status
    assert ErrorSerializer().serialize(exception, print_to_stdout=False) == exit_code
    assert cli._serialize_error(exception) == exit_code
    assert json.loads(capsys.readouterr().out)["exit_code"] == exit_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception",
    [
        RateLimitError("rate limited", "test"),
        ServiceUnavailable("unavailable", "test"),
        SDKTimeoutError(),
        SDKConnectionError(),
    ],
)
async def test_retry_handles_actual_sdk_qos_errors(exception: Exception) -> None:
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise exception
        return "ok"

    handler = RetryHandler(
        max_retries=2,
        base_delay=0,
        jitter=False,
        timeout_s=None,
    )
    assert await handler.execute(operation) == "ok"
    assert attempts == 3


@pytest.mark.asyncio
async def test_retry_does_not_retry_actual_sdk_auth_error() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise UnauthorizedError({})

    handler = RetryHandler(max_retries=2, base_delay=0, jitter=False, timeout_s=None)
    with pytest.raises(UnauthorizedError):
        await handler.execute(operation)
    assert attempts == 1


@pytest.mark.asyncio
async def test_exhausted_actual_sdk_503_maps_to_server_error() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise ServiceUnavailable("unavailable", "test")

    handler = RetryHandler(max_retries=1, base_delay=0, jitter=False, timeout_s=None)
    with pytest.raises(ServiceUnavailable) as captured:
        await handler.execute(operation)
    assert attempts == 2
    assert ErrorSerializer().serialize(captured.value, print_to_stdout=False) == 6


@pytest.mark.asyncio
async def test_non_bytes_streaming_result_is_server_contract_error(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = SimpleNamespace(streaming_continue=AsyncMock(return_value="not-bytes"))
    args = argparse.Namespace(
        parameter_inputs={},
        user_input={},
        contexts_override=None,
        message_id=None,
        session_trace_id=None,
        output_filename=None,
    )
    state = SessionState(
        "session",
        "agent",
        None,
        "2026-08-09T00:00:00+00:00",
        "2026-08-09T00:00:00+00:00",
        "active",
        [],
    )
    cfg = _Cfg(tmp_path)
    with pytest.raises(cli.SDKContractError) as captured:
        await cli._invoke_sdk(
            cli._spec_for("session", "streaming_continue"),
            client,
            args,
            30,
            cfg,
            state,
        )
    assert cli._serialize_error(captured.value) == 6
    assert json.loads(capsys.readouterr().out)["exit_code"] == 6


@pytest.mark.asyncio
async def test_invalid_json_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, *_ = _root()
    factory = _Factory(root)
    _patch_main(monkeypatch, tmp_path, factory)
    monkeypatch.setattr(sys, "argv", ["cmd", "session", "rag-context", "--alias", "a", "--parameter-inputs-json", "[]", "--user-input-json", "{}"])
    assert await cli.main() == 1
    assert json.loads(capsys.readouterr().out)["exit_code"] == 1
    assert factory.create_kwargs == {}
