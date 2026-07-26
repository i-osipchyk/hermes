from hermes import skilltools


def test_install_skills_into_project(tmp_path):
    target = skilltools.install_skills(dest=tmp_path)

    assert target == tmp_path / ".claude" / "skills"
    # Portable skills copied; hermes-extend deliberately excluded.
    for name in skilltools.PORTABLE_SKILLS:
        assert (target / name / "SKILL.md").exists()
    assert not (target / "hermes-extend").exists()

    # Reference docs travel alongside.
    assert (target / "hermes-reference" / "CONTEXT.md").exists()
    assert (target / "hermes-reference" / "docs" / "adr").is_dir()


def test_ported_skill_carries_reference_note(tmp_path):
    skilltools.install_skills(dest=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "hermes-strategy" / "SKILL.md").read_text()
    # Frontmatter preserved at the very top…
    assert text.startswith("---")
    # …and the ported note points at the bundled reference docs.
    assert "hermes-reference/" in text
    assert "Ported Hermes skill" in text


def test_frontmatter_still_parses_first(tmp_path):
    skilltools.install_skills(dest=tmp_path)
    text = (tmp_path / ".claude" / "skills" / "ask-hermes" / "SKILL.md").read_text()
    # The note must sit AFTER the closing frontmatter fence, not before it.
    first, second = [i for i, ln in enumerate(text.split("\n")) if ln.strip() == "---"][:2]
    assert first == 0
    note_line = next(i for i, ln in enumerate(text.split("\n")) if "Ported Hermes skill" in ln)
    assert note_line > second
