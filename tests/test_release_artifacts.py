from pathlib import Path


ROOT = Path(__file__).parent.parent


def test_public_distribution_metadata_and_commands_are_present():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "pal_found_cli"' in text
    assert "dynamic = [\"version\"]" in text
    assert 'version_file = "src/pal_found_cli/_version.py"' in text
    assert "pal-found-datasets" in text


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


def test_conda_publication_requires_credential_and_uploads_new_artifacts_in_order():
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    preflight = text.split("  preflight:", 1)[1].split("  release:", 1)[0]
    release = text.split("  release:", 1)[1].split("  conda:", 1)[0]
    conda = text.split("  conda:", 1)[1]
    upload = conda.split("- name: Publish conda package", 1)[1]

    assert "ANACONDA_API_TOKEN: ${{ secrets.ANACONDA_API_TOKEN }}" in preflight
    assert "exit 1" in preflight
    assert "needs: preflight" in release
    assert "needs: [preflight, release]" in conda
    assert 'test -f "$helper"' in upload
    assert 'test -f "$package"' in upload
    assert "toon-format-1.2.0-py_0.conda" in upload
    assert 'pal_found_cli-${GITHUB_REF_NAME#v}-py_0.conda' in upload
    assert upload.index('upload --user t-jet "$helper"') < upload.index(
        'upload --user t-jet "$package"'
    )
    assert "exit 1" in upload
    assert "--force" not in upload
