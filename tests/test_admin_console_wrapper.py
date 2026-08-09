import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from foundry_cli.admin.scripts import foundry_admin_cli


def test_packaged_module_exposes_operation_catalog():
    assert len(foundry_admin_cli.OP_SPECS) == 66
    assert (
        foundry_admin_cli.OPERATION_BY_RESOURCE["group_member"]["list"][
            "client_path"
        ]
        == "Group.GroupMember"
    )


def test_console_main_runs_async_main(monkeypatch):
    async def fake_main():
        return 7

    monkeypatch.setattr(foundry_admin_cli, "main", fake_main)

    assert foundry_admin_cli.console_main() == 7


@pytest.mark.asyncio
async def test_packaged_main_success(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.admin.User.get = AsyncMock(return_value={"rid": "x"})

    class Factory:
        def invocation_scope(self, cfg):
            class Scope:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return Scope()

        def create(self, cfg):
            return sdk

    class Cfg:
        timeout_s = 30
        log_level = "INFO"

        def load(self):
            return None

    retry = MagicMock()
    retry.execute = AsyncMock(return_value={"rid": "x"})
    monkeypatch.setattr(foundry_admin_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(foundry_admin_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_admin_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_admin_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(foundry_admin_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "user", "get", "user-id", "--format", "json"])

    rc = await foundry_admin_cli.main()

    assert rc == foundry_admin_cli.EXIT_SUCCESS
    assert "rid" in capsys.readouterr().out


def test_claude_launcher_imports_packaged_cli():
    import importlib.util
    from pathlib import Path

    launcher = (
        Path(__file__).parent.parent
        / ".claude"
        / "skills"
        / "foundry-admin"
        / "scripts"
        / "foundry_admin_cli.py"
    )
    spec = importlib.util.spec_from_file_location("foundry_admin_launcher", launcher)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.build_parser is foundry_admin_cli.build_parser
    assert module.console_main is foundry_admin_cli.console_main

