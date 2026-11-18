"""Run full installed-environment Safety scan with bounded BUG-051 CI policy."""

from datetime import date, datetime, timezone
from importlib import metadata
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
import yaml

ROOT = Path(__file__).resolve().parents[2]
IDS = {"SFTY-20261017-72390", "SFTY-20261017-54654", "SFTY-20261017-57659"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_context():
    require(platform.system() == "Linux", "BUG-051 requires Linux")
    require(platform.freedesktop_os_release().get("ID") == "ubuntu", "BUG-051 requires Ubuntu")
    for key, expected in {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": "Linux",
    }.items():
        require(os.environ.get(key) == expected, f"BUG-051 requires {key}={expected}")
    for package, expected in {"safety": "3.8.1", "filelock": "3.32.7"}.items():
        require(metadata.version(package) == expected, f"BUG-051 requires {package}=={expected}")

    # Follow active runtime and dev requirements, including propagated extras.
    pending = [("pal_found_cli", frozenset({"dev"}))]
    seen = set()
    while pending:
        package, extras = pending.pop()
        package = canonicalize_name(package)
        require(package != "filelock", "filelock entered product/dev dependency graph")
        if (package, extras) in seen:
            continue
        seen.add((package, extras))
        for raw in metadata.requires(package) or []:
            dependency = Requirement(raw)
            if dependency.marker is None or any(
                dependency.marker.evaluate({"extra": extra}) for extra in {"", *extras}
            ):
                pending.append((dependency.name, frozenset(dependency.extras)))


def build_policy(today):
    policy = yaml.safe_load((ROOT / ".safety-policy.yml").read_text())
    security = policy["security"]
    require(security["continue-on-vulnerability-error"] is False, "Safety must remain blocking")
    exceptions = yaml.safe_load((ROOT / ".github/safety-ci-exceptions.yml").read_text())
    require(set(exceptions) == IDS, "BUG-051 exception IDs changed; review required")
    require(all(entry["expires"] == "2026-12-02" for entry in exceptions.values()), "BUG-051 expiry changed")
    ignores = security["ignore-vulnerabilities"]
    require(not (IDS & ignores.keys()), "BUG-051 must not enter default local policy")
    for entry in [*ignores.values(), *exceptions.values()]:
        require(bool(entry["reason"].strip()), "Exception requires reason")
        require(today < date.fromisoformat(str(entry["expires"])), "Safety exception expired")
    ignores.update(exceptions)
    return policy


def main():
    check_context()
    policy = build_policy(datetime.now(timezone.utc).date())
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    temp_root = ROOT / ".ept/tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="safety-", dir=temp_root) as directory:
        path = Path(directory) / "policy.yml"
        path.write_text(yaml.safe_dump(policy), encoding="utf-8")
        # No requirements file or package filter: include scanner dependencies.
        return subprocess.run([
            sys.executable, "-m", "safety", "check", "--full-report", "--cache", "0",
            "--policy-file", str(path),
        ], cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main())
