#!/usr/bin/env python3
"""Unit tests for Foundry Admin CLI."""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

_SRC = Path(__file__).parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from foundry_cli.admin.scripts import foundry_admin_cli
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
    for spec in foundry_admin_cli.OP_SPECS:
        for name in spec["positional"] + spec["required"] + spec["optional"]:
            base.setdefault(name, None)
    base.update(kwargs)
    return argparse.Namespace(**base)


def _value_for(name):
    if name in foundry_admin_cli.JSON_ARGS:
        if name in {"attributes", "where", "initial_permissions"}:
            return '{"k": "v"}'
        return '["x"]'
    if name in foundry_admin_cli.BOOLEAN_ARGS:
        return True
    if name == "page_size":
        return 25
    if name == "page_token":
        return "tok"
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
    for spec in foundry_admin_cli.OP_SPECS:
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


def test_operation_catalog_has_66_unique_operations():
    paths = {
        (spec["resource"], spec["operation"])
        for spec in foundry_admin_cli.OP_SPECS
    }
    assert len(foundry_admin_cli.OP_SPECS) == 66
    assert len(paths) == 66


def test_catalog_matches_design_009_counts():
    expected = {
        "authentication_provider": 4,
        "cbac_banner": 1,
        "cbac_marking_restrictions": 1,
        "enrollment": 2,
        "enrollment_role_assignment": 3,
        "group": 8,
        "group_member": 3,
        "group_membership": 1,
        "group_membership_expiration_policy": 2,
        "group_provider_info": 2,
        "host": 1,
        "marking": 5,
        "marking_category": 4,
        "marking_member": 3,
        "marking_role_assignment": 3,
        "organization": 4,
        "organization_guest_member": 3,
        "organization_role_assignment": 3,
        "role": 2,
        "user": 9,
        "user_provider_info": 2,
    }
    actual = {
        resource: len(operations)
        for resource, operations in foundry_admin_cli.OPERATION_BY_RESOURCE.items()
    }
    assert actual == expected


@pytest.mark.parametrize("spec", foundry_admin_cli.OP_SPECS)
def test_parser_accepts_every_canonical_operation(spec):
    parser = foundry_admin_cli.build_parser()
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


@pytest.mark.parametrize("spec", foundry_admin_cli.OP_SPECS)
def test_operation_help_exits_zero(spec, capsys):
    parser = foundry_admin_cli.build_parser()
    argv = [
        spec["resource"].replace("_", "-"),
        spec["operation"].replace("_", "-"),
        "--help",
    ]

    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)

    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


@pytest.mark.parametrize("spec", foundry_admin_cli.OP_SPECS)
@pytest.mark.asyncio
async def test_invoke_dispatches_every_canonical_operation(spec):
    client = MagicMock()
    method = AsyncMock(return_value={"ok": True})
    setattr(client, spec["method"], method)
    args = _args_for(spec)

    await foundry_admin_cli._invoke(
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
    client.list = AsyncMock(return_value={"ok": True})
    args = _ns(
        group_id="group",
        principal_ids='["user-1", "user-2"]',
        include_expirations=True,
        transitive=True,
    )

    await foundry_admin_cli._invoke("group_member", "list", client, args, timeout=7)

    client.list.assert_awaited_once_with(
        "group",
        include_expirations=True,
        transitive=True,
        request_timeout=7,
    )


@pytest.mark.asyncio
async def test_invoke_decodes_all_design_json_args():
    for arg_name in foundry_admin_cli.JSON_ARGS:
        spec = next(spec for spec in foundry_admin_cli.OP_SPECS if arg_name in spec["json_args"])
        client = MagicMock()
        method = AsyncMock(return_value={"ok": True})
        setattr(client, spec["method"], method)
        args = _args_for(spec)

        await foundry_admin_cli._invoke(
            spec["resource"], spec["operation"], client, args, timeout=3
        )

        if arg_name in spec["positional"]:
            index = spec["positional"].index(arg_name)
            assert not isinstance(method.call_args.args[index], str)
        else:
            assert not isinstance(method.call_args.kwargs[arg_name], str)


def test_get_client_routes_admin_root_and_nested_clients():
    sdk = MagicMock()
    sdk.admin = _mock_root()
    factory = MagicMock()
    factory.create.return_value = sdk
    cfg = MagicMock()

    assert foundry_admin_cli._get_client(cfg, "user", factory) == sdk.admin.User
    assert (
        foundry_admin_cli._get_client(cfg, "group_member", factory)
        == sdk.admin.Group.GroupMember
    )
    assert (
        foundry_admin_cli._get_client(cfg, "authentication_provider", factory)
        == sdk.admin.Enrollment.AuthenticationProvider
    )


def test_paginated_catalog_matches_design_009():
    assert foundry_admin_cli.PAGINATED_OPS == {
        ("group", "list"),
        ("group", "search"),
        ("group_member", "list"),
        ("group_membership", "list"),
        ("host", "list"),
        ("marking", "list"),
        ("marking_category", "list"),
        ("marking_member", "list"),
        ("marking_role_assignment", "list"),
        ("user", "list"),
        ("user", "search"),
    }


@pytest.mark.parametrize("resource,operation", sorted(foundry_admin_cli.PAGINATED_OPS))
@pytest.mark.asyncio
async def test_invoke_paginated_uses_helper_for_all_paginated_ops(resource, operation):
    client = MagicMock()
    method_name = foundry_admin_cli._spec_for(resource, operation)["method"]
    method = MagicMock(
        return_value=_AsyncResourceIteratorDouble(
            [{"id": 1}, {"id": 2}, {"id": 3}],
            next_page_token="next",
        )
    )
    setattr(client, method_name, method)
    args = _args_for(foundry_admin_cli._spec_for(resource, operation), page_size=1)
    helper = foundry_admin_cli.PaginationHelper(page_size=1, batch_pages=2)

    result = await foundry_admin_cli._invoke_paginated(
        resource, operation, client, args, 3, helper
    )

    assert result == [{"id": 1}, {"id": 2}]
    method.assert_called_once()
    assert helper.pages_fetched == 2
    assert helper.next_page_token == "next"
    assert method.call_args.kwargs["page_size"] == 1
    assert method.call_args.kwargs["request_timeout"] == 3


@pytest.mark.asyncio
async def test_invoke_paginated_falls_back_to_page_envelopes():
    client = MagicMock()
    client.search = AsyncMock(
        side_effect=[
            {"items": [{"id": 1}], "next_page_token": "next"},
            {"items": [{"id": 2}]},
        ]
    )
    helper = foundry_admin_cli.PaginationHelper(page_size=1, batch_pages=2)

    result = await foundry_admin_cli._invoke_paginated(
        "user",
        "search",
        client,
        _ns(where="{}", page_size=1),
        3,
        helper,
    )

    assert result == [{"id": 1}, {"id": 2}]
    assert client.search.await_count == 2
    assert helper.pages_fetched == 2
    assert client.search.call_args_list[1].kwargs["page_token"] == "next"


@pytest.mark.asyncio
async def test_main_paginated_operation_emits_next_page_token(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.admin.Group.list = MagicMock(
        return_value=_AsyncResourceIteratorDouble(
            [{"rid": "one"}],
            next_page_token="next",
        )
    )
    factory = _ScopeFactory(sdk)
    monkeypatch.setattr(foundry_admin_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_admin_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_admin_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_admin_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "group", "list", "--page-size", "1", "--format", "json"],
    )

    rc = await foundry_admin_cli.main()

    captured = capsys.readouterr()
    assert rc == foundry_admin_cli.EXIT_SUCCESS
    assert json.loads(captured.out) == [{"rid": "one"}]
    assert "next_page_token" in captured.err
    assert "next" in captured.err


@pytest.mark.asyncio
async def test_main_success_uses_admin_acl_retry_output_and_b3_scope(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.admin.User.get = AsyncMock(return_value={"rid": "x"})
    factory = _ScopeFactory(sdk)
    guard = MagicMock()
    retry = MagicMock()
    retry.execute = AsyncMock(return_value={"rid": "x"})
    monkeypatch.setattr(foundry_admin_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_admin_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(foundry_admin_cli, "AccessControlGuard", lambda cfg, ns: guard)
    monkeypatch.setattr(foundry_admin_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(foundry_admin_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "user", "get", "user-id", "--format", "json"])

    rc = await foundry_admin_cli.main()

    assert rc == foundry_admin_cli.EXIT_SUCCESS
    guard.check.assert_called_once_with("user", "get")
    assert retry.execute.await_count == 1
    assert factory.entered is True
    assert "rid" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_main_acl_denied_returns_exit_8_and_skips_sdk(monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(foundry_admin_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_admin_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_admin_cli,
        "AccessControlGuard",
        lambda cfg, ns: MagicMock(
            check=MagicMock(
                side_effect=foundry_admin_cli.AccessControlError("blocked")
            )
        ),
    )
    monkeypatch.setattr(foundry_admin_cli, "AsyncClientFactory", factory)
    monkeypatch.setattr(sys, "argv", ["prog", "user", "get", "user-id"])

    rc = await foundry_admin_cli.main()

    assert rc == foundry_admin_cli.EXIT_ACCESS_CONTROL
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,exit_code",
    [
        (_HttpError(401), EXIT_AUTH),
        (FileNotFoundError("missing"), foundry_admin_cli.EXIT_NOT_FOUND),
        (
            foundry_admin_cli.AccessControlError("blocked"),
            foundry_admin_cli.EXIT_ACCESS_CONTROL,
        ),
        (EnvironmentError("bad config"), EXIT_CONFIGURATION),
    ],
)
async def test_main_returns_adr_001_exit_codes(monkeypatch, exc, exit_code):
    sdk = MagicMock()
    sdk.admin.User = MagicMock()
    factory = _ScopeFactory(sdk)
    retry = MagicMock()
    retry.execute = AsyncMock(side_effect=exc)
    monkeypatch.setattr(foundry_admin_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_admin_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_admin_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_admin_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(foundry_admin_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "user", "get", "user-id"])

    rc = await foundry_admin_cli.main()

    assert rc == exit_code


@pytest.mark.asyncio
async def test_main_user_profile_picture_bytes_output_is_length_envelope(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.admin.User.profile_picture = AsyncMock(return_value=b"abc")
    factory = _ScopeFactory(sdk)
    monkeypatch.setattr(foundry_admin_cli, "ConfigLoader", _Cfg)
    monkeypatch.setattr(foundry_admin_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_admin_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_admin_cli, "AsyncClientFactory", lambda: factory)
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "user", "profile-picture", "user-id", "--format", "json"],
    )

    rc = await foundry_admin_cli.main()

    assert rc == foundry_admin_cli.EXIT_SUCCESS
    assert json.loads(capsys.readouterr().out) == {"bytes": 3}
