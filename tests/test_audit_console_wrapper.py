"""Packaging and launcher tests for the Foundry Audit CLI."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from foundry_cli.audit.scripts import foundry_audit_cli

_ROOT = Path(__file__).parent.parent
_LAUNCHER = (
    _ROOT
    / ".claude"
    / "skills"
    / "foundry-audit"
    / "scripts"
    / "foundry_audit_cli.py"
)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_ROOT / "src")
    return env


def test_project_registers_exact_foundry_audit_console_entry() -> None:
    with (_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["scripts"]["foundry-audit"] == (
        "foundry_cli.audit.scripts.foundry_audit_cli:console_main"
    )


def test_console_main_uses_one_asyncio_run_boundary(monkeypatch) -> None:
    calls: list[object] = []

    async def fake_main() -> int:
        return 7

    def fake_run(awaitable):  # type: ignore[no-untyped-def]
        calls.append(awaitable)
        awaitable.close()
        return 7

    monkeypatch.setattr(foundry_audit_cli, "main", fake_main)
    monkeypatch.setattr(foundry_audit_cli.asyncio, "run", fake_run)
    assert foundry_audit_cli.console_main() == 7
    assert len(calls) == 1


def test_claude_launcher_is_thin_and_reexports_packaged_interfaces() -> None:
    spec = importlib.util.spec_from_file_location("foundry_audit_launcher", _LAUNCHER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.build_parser is foundry_audit_cli.build_parser
    assert module.main is foundry_audit_cli.main
    assert module.console_main is foundry_audit_cli.console_main
    source = _LAUNCHER.read_text(encoding="utf-8")
    assert "OP_SPECS" not in source
    assert "BinaryDownloadHandler" not in source
    assert "with_streaming_response" not in source


def test_imports_create_no_download_directory_or_network_side_effect(
    tmp_path: Path,
) -> None:
    code = (
        "import foundry_cli.audit; "
        "import foundry_cli.audit.scripts.foundry_audit_cli; "
        f"exec(compile(open({str(_LAUNCHER)!r}, encoding='utf-8').read(), "
        f"{str(_LAUNCHER)!r}, 'exec'), "
        f"{{'__name__': 'import_only', '__file__': {str(_LAUNCHER)!r}}})"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert list(tmp_path.iterdir()) == []


def test_packaged_module_help_returns_zero_and_lists_exact_operations() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "foundry_cli.audit.scripts.foundry_audit_cli",
            "--help",
        ],
        cwd=_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "log-file list" in completed.stdout
    assert "log-file content" in completed.stdout
    assert "FOUNDRY_TOKEN" not in completed.stderr


def test_claude_launcher_help_returns_zero_and_lists_exact_operations() -> None:
    completed = subprocess.run(
        [sys.executable, str(_LAUNCHER), "--help"],
        cwd=_ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "log-file list" in completed.stdout
    assert "log-file content" in completed.stdout
    assert "FOUNDRY_TOKEN" not in completed.stderr


def test_operation_help_surfaces_propagate_zero_exit_codes() -> None:
    for operation in ("list", "content"):
        completed = subprocess.run(
            [sys.executable, str(_LAUNCHER), "log-file", operation, "--help"],
            cwd=_ROOT,
            env=_subprocess_env(),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert "usage:" in completed.stdout
