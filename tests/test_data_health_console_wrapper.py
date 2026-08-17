"""Console and Claude launcher tests for Foundry Data Health."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pal_found_cli.data_health.scripts import pal_found_data_health_cli as packaged

_ROOT = Path(__file__).parent.parent


def test_console_main_owns_asyncio_boundary(monkeypatch) -> None:
    marker = object()
    monkeypatch.setattr(
        packaged.asyncio,
        "run",
        lambda coroutine: (coroutine.close(), marker)[1],
    )
    assert packaged.console_main() is marker


def test_console_entry_point_help_returns_zero_and_lists_operations() -> None:
    completed = subprocess.run(
        ["pal-found-data-health", "--help"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "check" in completed.stdout
    assert "FOUNDRY_TOKEN" not in completed.stderr
