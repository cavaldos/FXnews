"""Vendored markdown skills used as prompt knowledge.

Skills live under ``vibe/<skill-name>/`` (see ``vibe/NOTICE.md`` for the MIT
attribution to HKUDS/Vibe-Trading). Each skill's main file is ``SKILL.md``;
if that is missing, the first markdown file found is used.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

_VIBE_DIR = Path(__file__).resolve().parent / "vibe"
_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n?", re.DOTALL)


def list_skills() -> list[str]:
    """Return the sorted names of all vendored skills."""
    if not _VIBE_DIR.is_dir():
        return []
    return sorted(
        p.name
        for p in _VIBE_DIR.iterdir()
        if p.is_dir() and _main_file(p) is not None
    )


def _main_file(skill_dir: Path) -> Path | None:
    main = skill_dir / "SKILL.md"
    if main.is_file():
        return main
    candidates = sorted(p for p in skill_dir.rglob("*.md") if p.is_file())
    return candidates[0] if candidates else None


@functools.cache
def _read_skill(name: str) -> str:
    """Return the skill body with frontmatter stripped, or "" if unknown."""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return ""
    skill_dir = _VIBE_DIR / name
    if not skill_dir.is_dir():
        return ""
    path = _main_file(skill_dir)
    if path is None:
        return ""
    text = path.read_text(encoding="utf-8")
    return _FRONTMATTER_RE.sub("", text, count=1).strip()


def load_skill(name: str, max_chars: int = 6000) -> str:
    """Return the skill's main markdown (frontmatter removed), truncated.

    Unknown skill names return an empty string.
    """
    text = _read_skill(name)
    if max_chars is not None and max_chars >= 0:
        text = text[:max_chars]
    return text


def skills_block(names: list[str], max_chars_each: int = 3000) -> str:
    """Build a prompt block with one ``## Skill: <name>`` section per skill.

    Unknown or empty skills are skipped silently.
    """
    parts: list[str] = []
    for name in names:
        body = load_skill(name, max_chars=max_chars_each)
        if not body:
            continue
        parts.append(f"## Skill: {name}\n{body}\n\n")
    return "".join(parts)
