#!/usr/bin/env python3
"""Unit tests for Foundry Filesystem CLI."""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.filesystem.scripts import foundry_filesystem_cli
from foundry_cli.common.error_serializer import EXIT_AUTH, EXIT_CONFIGURATION


def _ns(**kwargs):
    base = {
        "timeout": None,
        "format": "auto",
        "pretty": False,
        "page_size": None,
        "page_token": None,
        "batch_pages": None,
    }
    for spec in foundry_filesystem_cli.OP_SPECS:
        for name in spec["positional"] + spec["required"] + spec["optional"]:
            base.setdefault(name, None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _value_for(name):
    if name in {
        "body",
        "default_roles",
        "deletion_policy_organizations",
        "marking_ids",
        "organization_rids",
        "organizations",
        "role_grants",
        "roles",
        "variable_values",
    }:
        return '["x"]'
    if name in {
        "include_inherited",
        "preview",
        "resource_level_role_grants_allowed",
    }:
        return True
    if name == "page_size":
        return 25
    if name == "page_token":
        return "tok"
    if name == "path":
        return "/My Project/folder"
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
    for spec in foundry_filesystem_cli.OP_SPECS:
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


class _AsyncResourceIteratorDouble:
    def __init__(self, items, next_page_token=None):
        self.items = list(items)
        self.index = 0
        self._page_iterator = self
        self._next_page_token = next_page_token

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self.index]
        self.index += 1
        return item

    async def get_next_page_token(self):
        return self._next_page_token


class _HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.response = MagicMock(status_code=status_code)


def test_operation_catalog_has_31_unique_operations():
    paths = {
        (spec["resource"], spec["operation"])
        for spec in foundry_filesystem_cli.OP_SPECS
    }
    assert len(foundry_filesystem_cli.OP_SPECS) == 31
    assert len(paths) == 31


@pytest.mark.parametrize("spec", foundry_filesystem_cli.OP_SPECS)
def test_parser_accepts_every_canonical_operation(spec):
    parser = foundry_filesystem_cli.build_parser()
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


@pytest.mark.parametrize("spec", foundry_filesystem_cli.OP_SPECS)
def test_operation_help_exits_zero(spec, capsys):
    parser = foundry_filesystem_cli.build_parser()
    argv = [
        spec["resource"].replace("_", "-"),
        spec["operation"].replace("_", "-"),
        "--help",
    ]

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)

    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("spec", foundry_filesystem_cli.OP_SPECS)
@pytest.mark.asyncio
async def test_invoke_dispatches_every_canonical_operation(spec):
    client = MagicMock()
    method = AsyncMock(return_value={"ok": True})
    setattr(client, spec["method"], method)
    args = _args_for(spec)

    await foundry_filesystem_cli._invoke(
        spec["resource"],
        spec["operation"],
        client,
        args,
        timeout=5,
    )

    assert method.await_count == 1
    assert method.call_args.kwargs["request_timeout"] == 5


def test_get_client_routes_resource_role_nested_client():
    sdk = MagicMock()
    sdk.filesystem = _mock_root()
    factory = MagicMock()
    factory.create.return_value = sdk
    cfg = MagicMock()

    assert (
        foundry_filesystem_cli._get_client(cfg, "resource_role", factory)
        == sdk.filesystem.Resource.Role
    )


def test_paginated_catalog_matches_page_size_and_token_specs():
    expected = {
        (spec["resource"], spec["operation"])
        for spec in foundry_filesystem_cli.OP_SPECS
        if "page_size" in spec["optional"] and "page_token" in spec["optional"]
    }
    assert foundry_filesystem_cli.PAGINATED_OPS == expected
    assert expected == {
        ("folder", "children"),
        ("project", "organizations"),
        ("resource", "markings"),
        ("resource_role", "list"),
        ("space", "list"),
    }


@pytest.mark.parametrize(
    "resource,operation,args",
    [
        ("folder", "children", _ns(folder_rid="folder", page_size=1)),
        ("project", "organizations", _ns(project_rid="project", page_size=1)),
        ("resource", "markings", _ns(resource_rid="resource", page_size=1)),
        (
            "resource_role",
            "list",
            _ns(resource_rid="resource", include_inherited=True, page_size=1),
        ),
        ("space", "list", _ns(page_size=1)),
    ],
)
@pytest.mark.asyncio
async def test_invoke_paginated_uses_helper_for_all_paginated_ops(
    resource, operation, args
):
    client = MagicMock()
    method = MagicMock(
        return_value=_AsyncResourceIteratorDouble(
            [{"id": 1}, {"id": 2}, {"id": 3}],
            next_page_token="next",
        )
    )
    setattr(client, foundry_filesystem_cli._spec_for(resource, operation)["method"], method)
    helper = foundry_filesystem_cli.PaginationHelper(page_size=1, batch_pages=2)

    result = await foundry_filesystem_cli._invoke_paginated(
        resource, operation, client, args, 3, helper
    )

    assert result == [{"id": 1}, {"id": 2}]
    method.assert_called_once()
    assert helper.pages_fetched == 2
    assert helper.next_page_token == "next"
    assert method.call_args_list[0].kwargs["page_size"] == 1
    assert method.call_args.kwargs["request_timeout"] == 3


@pytest.mark.asyncio
async def test_invoke_paginated_falls_back_to_page_envelopes():
    client = MagicMock()
    method = AsyncMock(
        side_effect=[
            {"items": [{"id": 1}], "next_page_token": "next"},
            {"items": [{"id": 2}]},
        ]
    )
    client.children = method
    helper = foundry_filesystem_cli.PaginationHelper(page_size=1, batch_pages=2)

    result = await foundry_filesystem_cli._invoke_paginated(
        "folder",
        "children",
        client,
        _ns(folder_rid="folder", page_size=1),
        3,
        helper,
    )

    assert result == [{"id": 1}, {"id": 2}]
    assert method.await_count == 2
    assert helper.pages_fetched == 2
    assert method.call_args_list[1].kwargs["page_token"] == "next"


@pytest.mark.asyncio
async def test_main_paginated_operation_emits_next_page_token(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.filesystem.Folder.children = MagicMock(
        return_value=_AsyncResourceIteratorDouble(
            [{"rid": "one"}],
            next_page_token="next",
        )
    )
    factory = _ScopeFactory(sdk)
    monkeypatch.setattr(foundry_filesystem_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_filesystem_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_filesystem_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_filesystem_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "folder",
            "children",
            "folder-rid",
            "--page-size",
            "1",
            "--format",
            "json",
        ],
    )

    rc = await foundry_filesystem_cli.main()

    captured = capsys.readouterr()
    assert rc == foundry_filesystem_cli.EXIT_SUCCESS
    assert json.loads(captured.out) == [{"rid": "one"}]
    assert "next_page_token" in captured.err
    assert "next" in captured.err


@pytest.mark.parametrize(
    "format_setting,expected",
    [
        ("json", '"key": "value"'),
        ("toon", "key"),
        ("auto", "a"),
    ],
)
def test_output_formatter_supports_json_toon_and_auto(format_setting, expected):
    data = (
        [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        if format_setting == "auto"
        else {"key": "value"}
    )

    output = foundry_filesystem_cli.OutputFormatter(
        format_setting=format_setting
    ).format(data)

    assert expected in output


@pytest.mark.asyncio
async def test_main_success_uses_acl_retry_output_and_b3_scope(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.filesystem.Resource.get = AsyncMock(return_value={"rid": "x"})
    factory = _ScopeFactory(sdk)
    guard = MagicMock()
    retry = MagicMock()
    retry.execute = AsyncMock(return_value={"rid": "x"})
    monkeypatch.setattr(foundry_filesystem_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_filesystem_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_filesystem_cli, "AccessControlGuard", lambda cfg, ns: guard
    )
    monkeypatch.setattr(foundry_filesystem_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(foundry_filesystem_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(
        sys, "argv", ["prog", "resource", "get", "resource-rid", "--format", "json"]
    )

    rc = await foundry_filesystem_cli.main()

    assert rc == foundry_filesystem_cli.EXIT_SUCCESS
    guard.check.assert_called_once_with("resource", "get")
    assert retry.execute.await_count == 1
    assert factory.entered is True
    assert "rid" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_acl_denied_returns_exit_8_and_skips_sdk(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(foundry_filesystem_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_filesystem_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_filesystem_cli,
        "AccessControlGuard",
        lambda cfg, ns: MagicMock(
            check=MagicMock(
                side_effect=foundry_filesystem_cli.AccessControlError("blocked")
            )
        ),
    )
    monkeypatch.setattr(foundry_filesystem_cli, "AsyncClientFactory", factory)
    monkeypatch.setattr(sys, "argv", ["prog", "resource", "get", "resource-rid"])

    rc = await foundry_filesystem_cli.main()

    assert rc == foundry_filesystem_cli.EXIT_ACCESS_CONTROL
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,exit_code",
    [
        (_HttpError(401), EXIT_AUTH),
        (FileNotFoundError("missing"), foundry_filesystem_cli.EXIT_NOT_FOUND),
        (
            foundry_filesystem_cli.AccessControlError("blocked"),
            foundry_filesystem_cli.EXIT_ACCESS_CONTROL,
        ),
        (EnvironmentError("bad config"), EXIT_CONFIGURATION),
    ],
)
async def test_main_returns_adr_001_exit_codes(monkeypatch, exc, exit_code):
    sdk = MagicMock()
    sdk.filesystem.Resource = MagicMock()
    factory = _ScopeFactory(sdk)
    retry = MagicMock()
    retry.execute = AsyncMock(side_effect=exc)
    monkeypatch.setattr(foundry_filesystem_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_filesystem_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_filesystem_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_filesystem_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(foundry_filesystem_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "resource", "get", "resource-rid"])

    rc = await foundry_filesystem_cli.main()

    assert rc == exit_code
