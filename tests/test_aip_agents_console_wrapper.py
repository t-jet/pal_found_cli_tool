"""Console and Claude launcher tests for Foundry AIP Agents."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from foundry_cli.aip_agents.scripts import foundry_aip_agents_cli as packaged


def test_console_main_owns_asyncio_boundary(monkeypatch) -> None:
    marker = object()
    monkeypatch.setattr(packaged.asyncio, "run", lambda coroutine: (coroutine.close(), marker)[1])
    assert packaged.console_main() is marker


def test_claude_launcher_delegates_packaged_interfaces() -> None:
    path = Path(".claude/skills/foundry-aip-agents/scripts/foundry_aip_agents_cli.py")
    spec = importlib.util.spec_from_file_location("aip_agents_launcher", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_parser is packaged.build_parser
    assert module.main is packaged.main
    assert module.console_main is packaged.console_main


def test_packaged_policy_does_not_depend_on_current_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert packaged._METADATA_ALLOWLIST_PATH.is_file()
