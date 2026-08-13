from pathlib import Path


ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".agents" / "skills"


def _namespace_skill_text(name: str) -> str:
    return (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")


def test_namespace_skills_document_capability_and_parameters() -> None:
    namespace_skills = sorted(
        path.name
        for path in SKILLS.glob("pal-found-*")
        if path.is_dir()
    )

    assert len(namespace_skills) == 18
    for name in namespace_skills:
        text = _namespace_skill_text(name)
        assert "## Capability and source" in text
        assert "Source:" in text
        assert "Parameters and JSON" in text


def test_json_parameter_docs_cover_namespace_specific_surfaces() -> None:
    expected_flags = {
        "pal-found-aip-agents": {
            "--parameter-inputs-json",
            "--user-input-json",
            "--contexts-override-json",
        },
        "pal-found-checkpoints": {"--records-json", "--where-json"},
        "pal-found-connectivity": {
            "--configuration-json",
            "--file-import-filters-json",
            "--secrets-json",
        },
        "pal-found-language-models": {
            "--messages-json",
            "--tools-json",
            "--input-json",
        },
        "pal-found-media-sets": {"--transformation-json"},
        "pal-found-models": {"--model-api-json", "--where-json"},
        "pal-found-orchestration": {"--target-json", "--where-json"},
        "pal-found-sql-queries": {
            "--fallback-branch-ids-json",
            "--parameters-json",
        },
        "pal-found-streams": {
            "--schema-json",
            "--records-json",
            "--offsets-json",
        },
        "pal-found-widgets": {"--settings-json"},
    }

    for name, flags in expected_flags.items():
        text = _namespace_skill_text(name)
        assert all(flag in text for flag in flags), name


def test_media_sets_document_variant_parameters() -> None:
    text = _namespace_skill_text("pal-found-media-sets")
    expected = {
        "--file",
        "--filename",
        "--media-item-path",
        "--media-item-rid",
        "--transaction-id",
        "--branch-name",
        "--branch-rid",
        "--view-rid",
        "--output",
        "--token",
        "--read-token",
        "--physical-item-name",
    }

    assert all(flag in text for flag in expected)
