"""Console and Claude launcher tests for Foundry Checkpoints."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from pal_found_cli.checkpoints.scripts import pal_found_checkpoints_cli as packaged

_ROOT = Path(__file__).parent.parent
_LAUNCHER = (
    _ROOT
    / ".agents"
    / "skills"
    / "pal-found-checkpoints"
    / "scripts"
    / "pal_found_checkpoints_cli.py"
)


def test_console_main_owns_asyncio_boundary(monkeypatch) -> None:
    marker = object()
    monkeypatch.setattr(
        packaged.asyncio,
        "run",
        lambda coroutine: (coroutine.close(), marker)[1],
    )
    assert packaged.console_main() is marker


def test_claude_launcher_delegates_without_business_logic() -> None:
    spec = importlib.util.spec_from_file_location("checkpoints_launcher", _LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_parser is packaged.build_parser
    assert module.main is packaged.main
    assert module.console_main is packaged.console_main


def test_claude_launcher_help_returns_zero_and_lists_exact_operations() -> None:
    completed = subprocess.run(
        [sys.executable, str(_LAUNCHER), "--help"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "record" in completed.stdout
    assert "FOUNDRY_TOKEN" not in completed.stderr
