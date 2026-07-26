"""Materialise the bundled Hermes skills into a project so they travel with the
library (ADR-0007).

Claude Code discovers skills from `.claude/skills/` (project or user), not from
installed packages — so `pip install hermes` bundles the skills in the wheel and this
copies them where Claude Code will find them. Sources resolve from the wheel's bundled
`_skills`/`_reference` when installed, or straight from the repo layout when running
editable. The docs the skills reference travel alongside as `hermes-reference/`, and a
note is added to each ported skill pointing there.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import hermes

# hermes-extend targets the library's own source, so it only makes sense in the repo.
PORTABLE_SKILLS = [
    "ask-hermes",
    "hermes-strategy",
    "hermes-explore-data",
    "hermes-backtest",
    "hermes-analyze-results",
]
_REFERENCE_ITEMS = ["CONTEXT.md", "docs/adr", "examples"]

_PKG = Path(hermes.__file__).parent


def _repo_root() -> Path:
    # editable install: src/hermes/__init__.py -> repo is two parents up
    return _PKG.parents[1]


def skills_source() -> Path:
    bundled = _PKG / "_skills"
    return bundled if bundled.exists() else _repo_root() / ".claude" / "skills"


def reference_source() -> Path:
    bundled = _PKG / "_reference"
    return bundled if bundled.exists() else _repo_root()


def _ported_note() -> str:
    return (
        "> **Ported Hermes skill.** Reference docs (CONTEXT.md, ADRs, examples) are in "
        "`.claude/skills/hermes-reference/`; the library is importable as `hermes` "
        "(inspect its API with `python -c \"import hermes; print(hermes.__all__)\"`)."
    )


def _inject_note(text: str) -> str:
    """Insert the ported-skill note just after the YAML frontmatter."""
    lines = text.split("\n")
    fences = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    note = ["", _ported_note(), ""]
    if len(fences) >= 2:
        at = fences[1] + 1
        lines[at:at] = note
        return "\n".join(lines)
    return "\n".join([*note, *lines])


def install_skills(dest: str | Path = ".", *, user: bool = False) -> Path:
    """Copy the portable skills + their reference docs into a target project (or the
    user-global skills dir). Returns the skills directory written to."""
    target = (Path.home() if user else Path(dest)) / ".claude" / "skills"
    target.mkdir(parents=True, exist_ok=True)

    src = skills_source()
    for name in PORTABLE_SKILLS:
        src_dir = src / name
        if not src_dir.exists():
            continue
        dst_dir = target / name
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)
        skill_md = dst_dir / "SKILL.md"
        if skill_md.exists():
            skill_md.write_text(_inject_note(skill_md.read_text()))

    ref_src = reference_source()
    ref_dst = target / "hermes-reference"
    ref_dst.mkdir(parents=True, exist_ok=True)
    for item in _REFERENCE_ITEMS:
        s = ref_src / item
        if not s.exists():
            continue
        d = ref_dst / item
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)

    return target
