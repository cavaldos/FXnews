from app.analysis import skills
from app.analysis.skills import list_skills, load_skill, skills_block

EXPECTED = {
    "global-macro",
    "macro-analysis",
    "sentiment-analysis",
    "event-driven",
    "geopolitical-risk",
    "smc",
    "ichimoku",
}


def test_list_skills_contains_vendored_set():
    names = list_skills()
    assert EXPECTED.issubset(set(names))
    assert names == sorted(names)


def test_load_skill_strips_frontmatter():
    text = load_skill("global-macro")
    assert text
    assert not text.startswith("---")
    assert "name: global-macro" not in text


def test_load_skill_truncates():
    text = load_skill("global-macro", max_chars=100)
    assert len(text) <= 100


def test_load_skill_unknown_returns_empty():
    assert load_skill("does-not-exist") == ""


def test_load_skill_rejects_path_traversal():
    assert load_skill("../skills") == ""
    assert load_skill("") == ""


def test_load_skill_falls_back_to_first_md(tmp_path, monkeypatch):
    skill_dir = tmp_path / "custom"
    (skill_dir / "refs").mkdir(parents=True)
    (skill_dir / "refs" / "a.md").write_text("---\nname: x\n---\nBody A", encoding="utf-8")
    monkeypatch.setattr(skills, "_VIBE_DIR", tmp_path)
    skills._read_skill.cache_clear()
    try:
        assert load_skill("custom") == "Body A"
        assert "custom" in list_skills()
    finally:
        skills._read_skill.cache_clear()


def test_skills_block_format_and_skips_unknown():
    block = skills_block(["smc", "nope"], max_chars_each=50)
    assert block.startswith("## Skill: smc\n")
    assert "## Skill: nope" not in block
    assert block.endswith("\n\n")
    assert len(block.split("\n", 1)[1].rstrip()) <= 50


def test_skills_block_empty_input():
    assert skills_block([]) == ""
