"""Console and Claude launcher tests for Foundry Third-Party Applications."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pal_found_cli.third_party_applications.scripts import (
    pal_found_third_party_applications_cli as packaged,
)

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
        ["pal-found-third-party-applications", "--help"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "third-party-application" in completed.stdout
    assert "FOUNDRY_TOKEN" not in completed.stderr
