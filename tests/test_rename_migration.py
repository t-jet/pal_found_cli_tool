from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parent.parent
MIGRATION = ROOT / ".ept" / "docs" / "deliverables" / "development" / "DEV-037-rename-migration.md"
SKILLS = ROOT / ".agents" / "skills"

NAMESPACES = (
    "datasets",
    "filesystem",
    "functions",
    "ontologies",
    "admin",
    "audit",
    "aip-agents",
    "language-models",
    "models",
    "orchestration",
    "sql-queries",
    "streams",
    "connectivity",
    "media-sets",
    "checkpoints",
    "data-health",
    "third-party-applications",
    "widgets",
)


def test_migration_guide_covers_confirmed_public_mapping() -> None:
    text = MIGRATION.read_text(encoding="utf-8")

    assert "pal_found_cli" in text
    assert "pal_found_cli_tool" in text
    assert "pal_found_cli_skills" in text
    assert "GitHub redirects" in text
    assert "## Rollback" in text
    for namespace in NAMESPACES:
        assert f"foundry-{namespace}" in text
        assert f"pal-found-{namespace}" in text


def test_public_package_and_entry_points_use_new_names() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'name = "pal_found_cli"' in pyproject
    assert 'source = ["pal_found_cli"]' in pyproject
    for namespace in NAMESPACES:
        assert f"pal-found-{namespace}" in pyproject
        assert f"pal_found_cli.{namespace.replace('-', '_')}" in pyproject
        assert f"foundry-{namespace} =" not in pyproject


def test_all_renamed_launchers_accept_help() -> None:
    for namespace in NAMESPACES:
        script = SKILLS / f"pal-found-{namespace}" / "scripts" / (
            f"pal_found_{namespace.replace('-', '_')}_cli.py"
        )
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout.lower()
