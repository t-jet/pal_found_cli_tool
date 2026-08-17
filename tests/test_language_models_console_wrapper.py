"""Console and packaging tests for Foundry Language Models."""

from __future__ import annotations

from pal_found_cli.language_models.scripts import pal_found_language_models_cli as packaged


def test_console_main_owns_asyncio_boundary(monkeypatch) -> None:
    marker = object()
    monkeypatch.setattr(packaged.asyncio, "run", lambda coroutine: (coroutine.close(), marker)[1])
    assert packaged.console_main() is marker


def test_packaged_policy_resolves_outside_repository(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert packaged._METADATA_ALLOWLIST_PATH.is_file()
