import asyncio

import pytest

from foundry_cli.datasets.scripts import foundry_datasets_cli


@pytest.fixture(autouse=True)
def restore_default_event_loop():
    yield
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)


def test_main_runs_async_entrypoint(monkeypatch):
    async def fake_async_main():
        return 23

    monkeypatch.setattr(foundry_datasets_cli, "async_main", fake_async_main)

    assert foundry_datasets_cli.main() == 23


def test_async_main_without_resource_returns_user_input_error(monkeypatch):
    monkeypatch.setattr("sys.argv", ["foundry-datasets"])

    assert (
        asyncio.run(foundry_datasets_cli.async_main())
        == foundry_datasets_cli.EXIT_USER_INPUT
    )
