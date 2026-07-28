import asyncio
from types import SimpleNamespace

import pytest

from foundry_cli.ontologies.scripts import foundry_ontologies_cli


@pytest.fixture(autouse=True)
def restore_default_event_loop():
    yield
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


def test_async_main_delegates_to_legacy_cli(monkeypatch):
    async def fake_main():
        return 17

    fake_module = SimpleNamespace(main=fake_main)
    monkeypatch.setattr(foundry_ontologies_cli, "_load_legacy_cli", lambda: fake_module)

    assert asyncio.run(foundry_ontologies_cli.async_main()) == 17


def test_main_runs_async_entrypoint(monkeypatch):
    async def fake_async_main():
        return 23

    monkeypatch.setattr(foundry_ontologies_cli, "async_main", fake_async_main)

    assert foundry_ontologies_cli.main() == 23


def test_load_legacy_cli_reports_missing_script(tmp_path, monkeypatch):
    foundry_ontologies_cli._load_legacy_cli.cache_clear()
    missing_script = tmp_path / "missing_cli.py"
    monkeypatch.setattr(foundry_ontologies_cli, "_LEGACY_SCRIPT", missing_script)

    with pytest.raises(ImportError, match="not found"):
        foundry_ontologies_cli._load_legacy_cli()

    foundry_ontologies_cli._load_legacy_cli.cache_clear()
