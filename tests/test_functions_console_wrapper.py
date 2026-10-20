import sys
from unittest.mock import Mock, AsyncMock, MagicMock

import pytest

from pal_found_cli.functions.scripts import pal_found_functions_cli


def test_packaged_module_exposes_operation_catalog():
    assert len(pal_found_functions_cli.OP_SPECS) == 7
    assert (
        pal_found_functions_cli.OPERATION_BY_RESOURCE["version_id"]["get"][
            "client_path"
        ]
        == "ValueType.VersionId"
    )


def test_console_main_runs_async_main(monkeypatch):
    async def fake_main():
        return 7

    monkeypatch.setattr(pal_found_functions_cli, "main", fake_main)

    assert pal_found_functions_cli.console_main() == 7


@pytest.mark.asyncio
async def test_packaged_main_success(monkeypatch, capsys):
    sdk = MagicMock()
    sdk.functions.Query.get = AsyncMock(return_value={"rid": "x"})

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
    monkeypatch.setattr(pal_found_functions_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(pal_found_functions_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        pal_found_functions_cli, "AccessControlGuard", lambda cfg, ns: MagicMock()
    )
    monkeypatch.setattr(pal_found_functions_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(pal_found_functions_cli, "RetryHandler", Mock(return_value=retry))
    monkeypatch.setattr(
        sys, "argv", ["prog", "query", "get", "query-name", "--format", "json"]
    )

    rc = await pal_found_functions_cli.main()
    pal_found_functions_cli.RetryHandler.assert_called_once_with(timeout_s=30)

    assert rc == pal_found_functions_cli.EXIT_SUCCESS
    assert "rid" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_streaming_query_preserves_ndjson_content(monkeypatch, capsys):
    ndjson = b'{"data":[1]}\n{"error":{"message":"partial failure"}}\n'

    class Cfg:
        timeout_s = 30
        log_level = "INFO"

        def load(self):
            return None

    class Factory:
        def invocation_scope(self, cfg):
            class Scope:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return Scope()

    retry = MagicMock()
    retry.execute = AsyncMock(return_value=ndjson)
    monkeypatch.setattr(pal_found_functions_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(pal_found_functions_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(
        pal_found_functions_cli,
        "AccessControlGuard",
        lambda cfg, ns: MagicMock(),
    )
    monkeypatch.setattr(pal_found_functions_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(pal_found_functions_cli, "_get_client", lambda *args: MagicMock())
    monkeypatch.setattr(pal_found_functions_cli, "RetryHandler", Mock(return_value=retry))
    monkeypatch.setattr(
        sys,
        "argv",
        ["prog", "query", "streaming-execute", "orders", "--parameters", "{}"],
    )

    assert await pal_found_functions_cli.main() == pal_found_functions_cli.EXIT_SUCCESS
    assert capsys.readouterr().out == ndjson.decode("utf-8")

