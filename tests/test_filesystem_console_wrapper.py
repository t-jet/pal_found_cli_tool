import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from foundry_cli.filesystem.scripts import foundry_filesystem_cli


def test_packaged_module_exposes_operation_catalog():
    assert len(foundry_filesystem_cli.OP_SPECS) == 31
    assert (
        foundry_filesystem_cli.OPERATION_BY_RESOURCE["resource_role"]["list"][
            "client_path"
        ]
        == "Resource.Role"
    )


def test_console_main_runs_async_main(monkeypatch):
    async def fake_main():
        return 31

    monkeypatch.setattr(foundry_filesystem_cli, "main", fake_main)

    assert foundry_filesystem_cli.console_main() == 31


@pytest.mark.asyncio
async def test_packaged_main_success(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.filesystem.Resource.get = AsyncMock(return_value={"rid": "x"})

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
    monkeypatch.setattr(foundry_filesystem_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(foundry_filesystem_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        foundry_filesystem_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(foundry_filesystem_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(foundry_filesystem_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(
        sys, "argv", ["prog", "resource", "get", "resource-rid", "--format", "json"]
    )

    rc = await foundry_filesystem_cli.main()

    assert rc == foundry_filesystem_cli.EXIT_SUCCESS
    assert "rid" in capsys.readouterr().out
