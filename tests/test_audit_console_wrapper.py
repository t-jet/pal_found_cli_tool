"""Packaging and launcher tests for the Foundry Audit CLI."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

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


def _isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def test_project_registers_exact_foundry_audit_console_entry() -> None:
    with (_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["scripts"]["foundry-audit"] == (
        "foundry_cli.audit.scripts.foundry_audit_cli:console_main"
    )
    assert project["tool"]["setuptools"]["package-data"]["foundry_cli.audit"] == [
        "metadata-allow-list.md"
    ]


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


@pytest.fixture(scope="module")
def isolated_package_environment(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("audit-package")
    wheel_dir = root / "wheelhouse"
    wheel_dir.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(_ROOT),
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
        ],
        cwd=root,
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheels = list(wheel_dir.glob("foundry_cli-*.whl"))
    assert len(wheels) == 1

    venv = root / "venv"
    create_venv = subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
        cwd=root,
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert create_venv.returncode == 0, create_venv.stderr
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    console = venv / ("Scripts/foundry-audit.exe" if os.name == "nt" else "bin/foundry-audit")
    arbitrary_cwd = root / "arbitrary-cwd"
    arbitrary_cwd.mkdir()
    return {
        "root": root,
        "wheel": wheels[0],
        "python": python,
        "console": console,
        "cwd": arbitrary_cwd,
    }


def _assert_installed_audit_smoke(environment: dict[str, Path]) -> None:
    env = _isolated_env()
    help_result = subprocess.run(
        [str(environment["console"]), "--help"],
        cwd=environment["cwd"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "log-file list" in help_result.stdout
    assert "log-file content" in help_result.stdout

    policy_code = """
import os
from foundry_cli.audit.scripts.foundry_audit_cli import _METADATA_ALLOWLIST_PATH
from foundry_cli.common.access_control_guard import AccessControlError, AccessControlGuard
from foundry_cli.common.config_loader import ConfigLoader
assert _METADATA_ALLOWLIST_PATH.is_file()
os.environ['FOUNDRY_AGENTIC_CLI_METADATA_ONLY'] = 'true'
guard = AccessControlGuard(
    ConfigLoader(),
    'AUDIT',
    metadata_allowlist_path=str(_METADATA_ALLOWLIST_PATH),
)
guard.check('log_file', 'list')
try:
    guard.check('log_file', 'content')
except AccessControlError:
    print('list=PERMITTED content=BLOCKED')
else:
    raise AssertionError('content unexpectedly permitted')
"""
    policy_result = subprocess.run(
        [str(environment["python"]), "-c", policy_code],
        cwd=environment["cwd"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert policy_result.returncode == 0, policy_result.stderr
    assert "list=PERMITTED content=BLOCKED" in policy_result.stdout

    launcher_result = subprocess.run(
        [str(environment["python"]), str(_LAUNCHER), "--help"],
        cwd=environment["cwd"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert launcher_result.returncode == 0, launcher_result.stderr
    assert "log-file list" in launcher_result.stdout
    assert "log-file content" in launcher_result.stdout


def test_wheel_and_editable_installs_work_from_arbitrary_cwd_without_pythonpath(
    isolated_package_environment: dict[str, Path],
) -> None:
    environment = isolated_package_environment
    with zipfile.ZipFile(environment["wheel"]) as archive:
        assert (
            "foundry_cli/audit/metadata-allow-list.md" in archive.namelist()
        )

    wheel_install = subprocess.run(
        [
            str(environment["python"]),
            "-m",
            "pip",
            "install",
            str(environment["wheel"]),
            "--no-deps",
            "--force-reinstall",
        ],
        cwd=environment["cwd"],
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert wheel_install.returncode == 0, wheel_install.stderr
    _assert_installed_audit_smoke(environment)

    editable_install = subprocess.run(
        [
            str(environment["python"]),
            "-m",
            "pip",
            "install",
            "--editable",
            str(_ROOT),
            "--no-deps",
            "--force-reinstall",
        ],
        cwd=environment["cwd"],
        env=_isolated_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert editable_install.returncode == 0, editable_install.stderr
    _assert_installed_audit_smoke(environment)
