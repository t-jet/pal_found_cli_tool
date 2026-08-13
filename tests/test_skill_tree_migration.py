from pathlib import Path


ROOT = Path(__file__).parent.parent
SKILLS = ROOT / ".agents" / "skills"

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
    actual = {
        path.name
        for path in SKILLS.iterdir()
        if path.is_dir() and path.name != "self-improvement"
    }
    assert actual == EXPECTED_SKILLS
    assert not (ROOT / ".claude" / "skills" / "pal-found-datasets").exists()

    for name in EXPECTED_SKILLS:
        skill_dir = SKILLS / name
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
    legacy = ROOT / ".claude" / "skills"
    assert [path.name for path in legacy.iterdir()] == ["README.md"]
    assert ".agents/skills" in (legacy / "README.md").read_text(encoding="utf-8")
