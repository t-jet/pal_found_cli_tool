"""Bounds of the BUG-051 CI exception proposal."""

from datetime import date
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/check_safety.py"
spec = importlib.util.spec_from_file_location("check_safety", SCRIPT)
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(scanner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(scanner.platform, "freedesktop_os_release", lambda: {"ID": "ubuntu"})
    for key, value in {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(scanner.metadata, "version", lambda name: {"safety": "3.8.1", "filelock": "3.32.7"}[name])
    monkeypatch.setattr(scanner.metadata, "requires", lambda name: [])


def test_policy_preserves_default_and_has_only_reviewed_additions():
    before = (scanner.ROOT / ".safety-policy.yml").read_bytes()
    policy = scanner.build_policy(date(2026, 11, 18))
    original = yaml.safe_load(before)
    ignores = policy["security"]["ignore-vulnerabilities"]
    assert set(ignores) - original["security"]["ignore-vulnerabilities"].keys() == scanner.IDS
    assert ignores["SFTY-20260902-58666"] == original["security"]["ignore-vulnerabilities"]["SFTY-20260902-58666"]
    assert policy["security"]["continue-on-vulnerability-error"] is False
    assert (scanner.ROOT / ".safety-policy.yml").read_bytes() == before


@pytest.mark.parametrize("today", [date(2026, 12, 2), date(2026, 12, 3)])
def test_expired_exception_blocks_even_without_findings(today):
    with pytest.raises(RuntimeError, match="expired"):
        scanner.build_policy(today)


@pytest.mark.parametrize("key,value", [("GITHUB_ACTIONS", "false"), ("RUNNER_ENVIRONMENT", "self-hosted"), ("RUNNER_OS", "Windows")])
def test_runner_mismatch_blocks(context, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError):
        scanner.check_context()


@pytest.mark.parametrize("system,os_id", [("Windows", "ubuntu"), ("Linux", "debian")])
def test_actual_platform_mismatch_blocks(context, monkeypatch, system, os_id):
    monkeypatch.setattr(scanner.platform, "system", lambda: system)
    monkeypatch.setattr(scanner.platform, "freedesktop_os_release", lambda: {"ID": os_id})
    with pytest.raises(RuntimeError):
        scanner.check_context()


@pytest.mark.parametrize("changed", ["safety", "filelock"])
def test_version_mismatch_blocks(context, monkeypatch, changed):
    expected = {"safety": "3.8.1", "filelock": "3.32.7"}
    expected[changed] = "999"
    monkeypatch.setattr(scanner.metadata, "version", expected.__getitem__)
    with pytest.raises(RuntimeError):
        scanner.check_context()


@pytest.mark.parametrize("requirements", [
    {"pal-found-cli": ["filelock"]},
    {"pal-found-cli": ['example[locks]; extra == "dev"'], "example": ['filelock; extra == "locks"']},
])
def test_product_or_dev_filelock_blocks(context, monkeypatch, requirements):
    monkeypatch.setattr(scanner.metadata, "requires", lambda name: requirements.get(name, []))
    with pytest.raises(RuntimeError, match="product/dev"):
        scanner.check_context()


@pytest.mark.parametrize("code", [0, 64, 1])
def test_full_scan_propagates_unsuppressed_and_operational_failures(context, monkeypatch, code):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if "safety" in argv:
            generated = yaml.safe_load(Path(argv[-1]).read_text())
            assert generated["security"]["continue-on-vulnerability-error"] is False
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(scanner.subprocess, "run", run)
    assert scanner.main() == code
    assert calls[0] == [scanner.sys.executable, "-m", "pip", "check"]
    assert calls[1][:-1] == [scanner.sys.executable, "-m", "safety", "check", "--full-report", "--cache", "0", "--policy-file"]


def test_guard_failure_never_launches_scanner(monkeypatch):
    monkeypatch.setattr(scanner.platform, "system", lambda: "Windows")
    monkeypatch.setattr(scanner.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not scan"))
    with pytest.raises(RuntimeError):
        scanner.main()
