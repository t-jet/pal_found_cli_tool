#!/usr/bin/env python3
"""Unit tests for Foundry Functions CLI."""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.common.error_serializer import EXIT_AUTH, EXIT_CONFIGURATION
from foundry_cli.functions.scripts import foundry_functions_cli


def _ns(**kwargs):
    base = {
        "timeout": None,
        "format": "auto",
        "pretty": False,
    }
    for spec in foundry_functions_cli.OP_SPECS:
        for name in spec["positional"] + spec["required"] + spec["optional"]:
            base.setdefault(name, None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _value_for(name):
    if name == "parameters":
        return '{"p": 1}'
    if name == "attribution":
        return '{"source": "unit-test"}'
    if name == "body":
        return '[{"rid": "ri.function.main"}]'
    if name in {"include_prerelease", "preview"}:
        return True
    return f"{name}-value"


def _args_for(spec, **overrides):
    values = {
        name: _value_for(name)
        for name in spec["positional"] + spec["required"] + spec["optional"]
    }
    values.update(overrides)
    return _ns(**values)


def _mock_root():
    root = MagicMock()
    for spec in foundry_functions_cli.OP_SPECS:
        client = root
        for attr in spec["client_path"].split("."):
            client = getattr(client, attr)
        setattr(client, spec["method"], AsyncMock(return_value={"ok": True}))
    return root


class _Cfg:
    timeout_s = 30
    log_level = "INFO"

    def load(self):
        return None


class _ScopeFactory:
    def __init__(self, sdk):
        self.sdk = sdk
        self.entered = False

    def invocation_scope(self, cfg):
        factory = self

        class Scope:
            def __enter__(self):
                factory.entered = True

            def __exit__(self, exc_type, exc, tb):
                return False

        return Scope()

    def create(self, cfg):
        return self.sdk


class _HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = MagicMock(status_code=status_code)


def test_operation_catalog_has_7_unique_operations():
    paths = {
        (spec["resource"], spec["operation"])
        for spec in foundry_functions_cli.OP_SPECS
    }
    assert len(foundry_functions_cli.OP_SPECS) == 7
    assert len(paths) == 7
    assert foundry_functions_cli.PAGINATED_OPS == frozenset()


@pytest.mark.parametrize("spec", foundry_functions_cli.OP_SPECS)
def test_parser_accepts_every_canonical_operation(spec):
    parser = foundry_functions_cli.build_parser()
    argv = [spec["resource"].replace("_", "-"), spec["operation"].replace("_", "-")]
    for arg in spec["positional"]:
        argv.append(_value_for(arg))
    for arg in spec["required"]:
        argv.extend(["--" + arg.replace("_", "-"), _value_for(arg)])
    argv.extend(["--timeout", "9", "--format", "json"])

    args = parser.parse_args(argv)

    assert args.resource == spec["resource"].replace("_", "-")
    assert args.operation == spec["operation"].replace("_", "-")
    assert args.timeout == 9
    assert args.format == "json"


@pytest.mark.parametrize("spec", foundry_functions_cli.OP_SPECS)
def test_operation_help_exits_zero(spec, capsys):
    parser = foundry_functions_cli.build_parser()
    argv = [
        spec["resource"].replace("_", "-"),
        spec["operation"].replace("_", "-"),
        "--help",
    ]

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)

    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("spec", foundry_functions_cli.OP_SPECS)
@pytest.mark.asyncio
async def test_invoke_dispatches_every_canonical_operation(spec):
    client = MagicMock()
    method = AsyncMock(return_value={"ok": True})
    setattr(client, spec["method"], method)
    args = _args_for(spec)

    await foundry_functions_cli._invoke(
        spec["resource"],
        spec["operation"],
        client,
        args,
        timeout=5,
    )

    assert method.await_count == 1
    assert method.call_args.kwargs["request_timeout"] == 5


@pytest.mark.asyncio
async def test_invoke_parses_json_and_boolean_args():
    client = MagicMock()
    client.execute = AsyncMock(return_value={"ok": True})
    args = _ns(
        query_api_name="query",
        parameters='{"x": 2}',
        attribution='{"service": "cli"}',
        preview=True,
    )

    await foundry_functions_cli._invoke("query", "execute", client, args, timeout=7)

    client.execute.assert_awaited_once_with(
        "query",
        parameters={"x": 2},
        attribution={"service": "cli"},
        preview=True,
        request_timeout=7,
    )


@pytest.mark.asyncio
async def test_invoke_parses_body_json_for_batch():
    client = MagicMock()
    client.get_by_rid_batch = AsyncMock(return_value={"ok": True})
    args = _ns(body='[{"rid": "ri.function.main"}]', preview=True)

    await foundry_functions_cli._invoke(
        "query", "get_by_rid_batch", client, args, timeout=3
    )

    client.get_by_rid_batch.assert_awaited_once_with(
        [{"rid": "ri.function.main"}],
        preview=True,
        request_timeout=3,
    )


def test_get_client_routes_root_and_nested_clients():
    sdk = MagicMock()
    sdk.functions = _mock_root()
    factory = MagicMock()
    factory.create.return_value = sdk
    cfg = MagicMock()

    assert foundry_functions_cli._get_client(cfg, "query", factory) == sdk.functions.Query
    assert (
        foundry_functions_cli._get_client(cfg, "version_id", factory)
        == sdk.functions.ValueType.VersionId
    )


def test_model_to_dict_converts_bytes_to_length_envelope():
    assert foundry_functions_cli._model_to_dict(b"abc") == {"bytes": 3}


@pytest.mark.asyncio
async def test_main_success_uses_acl_retry_output_and_b3_scope(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.functions.Query.get = AsyncMock(return_value={"rid": "x"})
    factory = _ScopeFactory(sdk)
    guard = MagicMock()
    retry = MagicMock()
    retry.execute = AsyncMock(return_value={"rid": "x"})
    monkeypatch.setattr(foundry_functions_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_functions_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_functions_cli, "AccessControlGuard", lambda cfg, ns: guard
    )
    monkeypatch.setattr(foundry_functions_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(foundry_functions_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(
        sys, "argv", ["prog", "query", "get", "query-name", "--format", "json"]
    )

    rc = await foundry_functions_cli.main()

    assert rc == foundry_functions_cli.EXIT_SUCCESS
    guard.check.assert_called_once_with("query", "get")
    assert retry.execute.await_count == 1
    assert factory.entered is True
    assert "rid" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_acl_denied_returns_exit_8_and_skips_sdk(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(foundry_functions_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_functions_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_functions_cli,
        "AccessControlGuard",
        lambda cfg, ns: MagicMock(
            check=MagicMock(
                side_effect=foundry_functions_cli.AccessControlError("blocked")
            )
        ),
    )
    monkeypatch.setattr(foundry_functions_cli, "AsyncClientFactory", factory)
    monkeypatch.setattr(sys, "argv", ["prog", "query", "get", "query-name"])

    rc = await foundry_functions_cli.main()

    assert rc == foundry_functions_cli.EXIT_ACCESS_CONTROL
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,exit_code",
    [
        (_HttpError(401), EXIT_AUTH),
        (FileNotFoundError("missing"), foundry_functions_cli.EXIT_NOT_FOUND),
        (
            foundry_functions_cli.AccessControlError("blocked"),
            foundry_functions_cli.EXIT_ACCESS_CONTROL,
        ),
        (EnvironmentError("bad config"), EXIT_CONFIGURATION),
    ],
)
async def test_main_returns_adr_001_exit_codes(monkeypatch, exc, exit_code):
    sdk = MagicMock()
    sdk.functions.Query = MagicMock()
    factory = _ScopeFactory(sdk)
    retry = MagicMock()
    retry.execute = AsyncMock(side_effect=exc)
    monkeypatch.setattr(foundry_functions_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_functions_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_functions_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_functions_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(foundry_functions_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "query", "get", "query-name"])

    rc = await foundry_functions_cli.main()

    assert rc == exit_code


@pytest.mark.asyncio
async def test_main_streaming_bytes_output_is_length_envelope(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.functions.Query.streaming_execute = AsyncMock(return_value=b"abc")
    factory = _ScopeFactory(sdk)
    monkeypatch.setattr(foundry_functions_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_functions_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_functions_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_functions_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "query",
            "streaming-execute",
            "query-name",
            "--parameters",
            "{}",
            "--format",
            "json",
        ],
    )

    rc = await foundry_functions_cli.main()

    assert rc == foundry_functions_cli.EXIT_SUCCESS
    assert json.loads(capsys.readouterr().out) == {"bytes": 3}

