from pathlib import Path


ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".agents" / "skills"
LEGACY = ROOT / ".claude" / "skills"
DISTRIBUTION_README = ROOT / "pal_found_cli_skills" / "README.md"

EXPECTED_SKILLS = {
    "pal-found",
    "pal-found-admin",
    "pal-found-aip-agents",
    "pal-found-audit",
    "pal-found-checkpoints",
    "pal-found-connectivity",
    "pal-found-data-health",
    "pal-found-datasets",
    "pal-found-filesystem",
    "pal-found-functions",
    "pal-found-language-models",
    "pal-found-media-sets",
    "pal-found-models",
    "pal-found-ontologies",
    "pal-found-orchestration",
    "pal-found-sql-queries",
    "pal-found-streams",
    "pal-found-third-party-applications",
    "pal-found-widgets",
}

SUPPORTED_HARNESSES = {"Codex", "Claude Code"}


def _frontmatter_name(skill_file: Path) -> str:
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "---"
    for line in lines[1:]:
        if line == "---":
            break
        if line.startswith("name:"):
            return line.partition(":")[2].strip()
    raise AssertionError(f"No name field in {skill_file}")


def test_canonical_skill_tree_has_one_renamed_skill_set() -> None:
    assert SKILLS.is_dir()
    actual = {
        path.name
        for path in SKILLS.iterdir()
        if path.is_dir() and path.name != "self-improvement"
    }
    assert len(actual) == 19
    assert actual == EXPECTED_SKILLS

    for name in EXPECTED_SKILLS:
        skill_dir = SKILLS / name
        assert skill_dir.is_dir()
        assert (skill_dir / "SKILL.md").is_file()
        assert _frontmatter_name(skill_dir / "SKILL.md") == name
        text = "\n".join(
            file.read_text(encoding="utf-8")
            for file in skill_dir.rglob("*")
            if file.is_file() and "__pycache__" not in file.parts
        )
        assert ".claude/skills" not in text
        assert "src/foundry_cli" not in text
        assert "from foundry_cli" not in text


def test_namespace_launchers_use_pal_found_names() -> None:
    for skill_dir in SKILLS.iterdir():
        if skill_dir.name in {"pal-found", "self-improvement"}:
            continue
        namespace = skill_dir.name.removeprefix("pal-found-").replace("-", "_")
        launchers = list((skill_dir / "scripts").glob("*.py"))
        assert [path.name for path in launchers] == [
            f"pal_found_{namespace}_cli.py"
        ]


def test_legacy_skill_path_is_pointer_only() -> None:
    assert LEGACY.is_dir()
    assert [path.name for path in LEGACY.iterdir()] == ["README.md"]
    pointer = (LEGACY / "README.md").read_text(encoding="utf-8")
    assert ".agents/skills" in pointer
    assert "canonical" in pointer.lower()


def test_distribution_readme_documents_supported_harness_onboarding() -> None:
    text = DISTRIBUTION_README.read_text(encoding="utf-8")

    assert "## Supported harnesses" in text
    assert SUPPORTED_HARNESSES <= {
        line.split("|")[1].strip()
        for line in text.splitlines()
        if line.startswith("|") and line.count("|") >= 4
    }
    assert "19 skills" in text
    assert ".agents/skills" in text
    assert ".claude/skills" in text
    assert "ln -s" in text
    assert "New-Item -ItemType Junction" in text
    for harness in SUPPORTED_HARNESSES:
        assert f"Start a new {harness} session" in text
    assert "git clone" in text
    assert "git pull --ff-only" in text

    for name in EXPECTED_SKILLS:
        assert f"`{name}`" in text
