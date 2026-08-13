from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_public_distribution_metadata_and_commands_are_present():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "pal_found_cli"' in text
    assert "dynamic = [\"version\"]" in text
    assert 'version_file = "src/pal_found_cli/_version.py"' in text
    assert "pal-found-datasets" in text


def test_repository_urls_use_final_names():
    text = (ROOT / ".gitmodules").read_text(encoding="utf-8")
    assert "pal_found_cli_tool.git" in text
    assert "pal_found_cli_skills.git" in text
    assert "foundry_cli_tool.git" not in text
    assert "foundry_cli_skills.git" not in text


def test_release_workflow_has_staging_and_oidc_gate():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "test.pypi.org" in text
    assert "id-token: write" in text
    assert "PYPI_API_TOKEN" not in text
    assert "Verify staged release in clean environment" in text


def test_conda_recipe_aligns_name_and_tag_version():
    text = (ROOT / "conda.recipe" / "meta.yaml").read_text(encoding="utf-8")
    assert "name: pal_found_cli" in text
    assert "GIT_TAG" in text
    assert "pal-found-datasets" in text


def test_skills_distribution_readme_has_clone_copy_update_flow():
    text = (ROOT / "pal_found_cli_skills" / "README.md").read_text(encoding="utf-8")
    for marker in ("git clone", ".agents/skills", "Claude Code", "## Update"):
        assert marker in text
