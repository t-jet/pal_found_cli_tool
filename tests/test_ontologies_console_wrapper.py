import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from foundry_cli.ontologies.scripts import foundry_ontologies_cli


def test_packaged_module_exposes_operation_catalog():
    assert len(foundry_ontologies_cli.OP_SPECS) == 67
    assert foundry_ontologies_cli.OPERATION_BY_RESOURCE["ontology"]["get"]["method"] == "get"


def test_console_main_runs_async_main(monkeypatch):
    async def fake_main():
        return 31

    monkeypatch.setattr(foundry_ontologies_cli, "main", fake_main)

    assert foundry_ontologies_cli.console_main() == 31


@pytest.mark.asyncio
async def test_packaged_main_success(monkeypatch, capsys):
    client = MagicMock()
    client.get = AsyncMock(return_value={"rid": "x"})

    class Factory:
        def invocation_scope(self, cfg):
            class Scope:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return Scope()

        def create(self, cfg):
            sdk = MagicMock()
            sdk.ontologies.Ontology = client
            return sdk

    class Cfg:
        timeout_s = 30
        log_level = "INFO"

        def load(self):
            return None

    retry = MagicMock()
    retry.execute = AsyncMock(return_value={"rid": "x"})
    monkeypatch.setattr(foundry_ontologies_cli, "ConfigLoader", Cfg)
    monkeypatch.setattr(foundry_ontologies_cli, "LogSetup", MagicMock())
    monkeypatch.setattr(foundry_ontologies_cli, "AccessControlGuard", lambda cfg, ns: MagicMock())
    monkeypatch.setattr(foundry_ontologies_cli, "AsyncClientFactory", Factory)
    monkeypatch.setattr(foundry_ontologies_cli, "RetryHandler", lambda: retry)
    monkeypatch.setattr(sys, "argv", ["prog", "ontology", "get", "ontology-rid", "--format", "json"])

    rc = await foundry_ontologies_cli.main()

    assert rc == foundry_ontologies_cli.EXIT_SUCCESS
    assert "rid" in capsys.readouterr().out
