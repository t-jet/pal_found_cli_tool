"""Console and packaging tests for Foundry Language Models."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from pal_found_cli.language_models.scripts import pal_found_language_models_cli as packaged


def test_console_main_owns_asyncio_boundary(monkeypatch) -> None:
    marker = object()
    monkeypatch.setattr(packaged.asyncio, "run", lambda coroutine: (coroutine.close(), marker)[1])
    assert packaged.console_main() is marker


def test_claude_launcher_delegates_without_business_logic() -> None:
    path = Path(".agents/skills/pal-found-language-models/scripts/pal_found_language_models_cli.py")
    spec = importlib.util.spec_from_file_location("language_models_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_parser is packaged.build_parser
    assert module.main is packaged.main
    assert module.console_main is packaged.console_main


def test_packaged_policy_resolves_outside_repository(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert packaged._METADATA_ALLOWLIST_PATH.is_file()
