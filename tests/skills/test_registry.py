"""SkillRegistry loading and rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.skills.discovery import SkillRoot
from deeppyro.skills.errors import SkillNotFoundError
from deeppyro.skills.registry import (
    SkillRegistry,
    catalog_description,
    render_skill_content,
)
from deeppyro.skills.types import (
    CUSTOM_RANK,
    SOURCE_CUSTOM,
    SOURCE_USER_DSH,
    USER_DSH_RANK,
    SkillConfig,
    SkillDefinition,
)

from .helpers import make_project, write_skill


def _registry(path: Path) -> SkillRegistry:
    root = SkillRoot(rank=CUSTOM_RANK, source=SOURCE_CUSTOM, path=Path(path).resolve())
    return SkillRegistry(roots=[root])


def test_list_get_and_read(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    path = write_skill(skills, "demo", description="Demo skill", body="Step one.\nStep two.")
    registry = _registry(skills)
    assert [item.name for item in registry.list()] == ["demo"]
    definition = registry.get("demo")
    assert definition is not None
    assert definition.content == "Step one.\nStep two."
    assert definition.path == str(path)
    assert registry.read("demo").content.startswith("Step one.")
    assert registry.snapshot().errors == ()


def test_get_rejects_invalid_and_unknown_names(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    write_skill(skills, "demo")
    registry = _registry(skills)
    assert registry.get("Demo") is None
    assert registry.get("missing") is None
    with pytest.raises(SkillNotFoundError, match="unknown or no longer available"):
        registry.read("missing")


def test_get_returns_none_when_file_disappears(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    path = write_skill(skills, "demo")
    registry = _registry(skills)
    path.unlink()
    assert registry.get("demo") is None


def test_render_catalog_empty_catalog_is_empty_string(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "missing")
    assert registry.render_catalog() == ""


def test_render_catalog_lists_model_invocable_skills_sorted(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    write_skill(skills, "zeta", description="Zeta skill")
    write_skill(skills, "alpha", description="Alpha skill")
    write_skill(skills, "hidden", description="Not for models", extra="disable-model-invocation: true\n")
    text = _registry(skills).render_catalog()
    assert text.startswith("<system-reminder>")
    assert "<available_skills>" in text
    assert text.index("- alpha:") < text.index("- zeta:")
    assert "hidden" not in text
    assert "This catalog contains summaries only" in text


def test_render_catalog_truncates_descriptions_and_entries(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    write_skill(skills, "alpha", description="x" * 40)
    write_skill(skills, "beta", description="Beta skill")
    registry = _registry(skills)
    text = registry.render_catalog(description_max_length=20, max_entries=1)
    assert "x" * 17 + "..." in text
    assert "- beta:" not in text


def test_render_catalog_escapes_description_markup(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    write_skill(skills, "alpha", description="Use <tag> & friends")
    text = _registry(skills).render_catalog()
    assert "Use &lt;tag&gt; &amp; friends" in text
    assert "<tag>" not in text


def test_catalog_description_normalizes_and_clamps() -> None:
    assert catalog_description("  a   b \n c  ") == "a b c"
    assert catalog_description("abcdef", 5) == "ab..."
    assert catalog_description("abcdef", 1) == "..."
    assert catalog_description("abcdef", 3) == "..."


def test_render_skill_content_with_base_directory() -> None:
    definition = SkillDefinition(
        name="demo",
        description="Demo",
        path="/skills/demo/SKILL.md",
        resource_base="/skills/demo",
        content="Do it.",
    )
    text = render_skill_content(definition)
    assert text.startswith('<skill_content name="demo">')
    assert "Base directory for this skill: /skills/demo" in text
    assert "<skill_instructions>\nDo it.\n</skill_instructions>" in text
    assert "Resolve relative paths mentioned by this skill" in text


def test_render_skill_content_provider_fallback() -> None:
    definition = SkillDefinition(
        name="virtual", description="Virtual", provider="remote-provider", content="Body."
    )
    text = render_skill_content(definition)
    assert 'Resources for this skill are managed by provider "remote-provider".' in text
    assert "Base directory for this skill" not in text


def test_explicit_roots_ignore_cwd_and_report_duplicates(tmp_path: Path, isolated_home: Path) -> None:
    project = make_project(tmp_path)
    write_skill(project / ".dsh" / "skills", "shared", description="Project copy")
    write_skill(isolated_home / "skills", "shared", description="User copy")
    roots = [
        SkillRoot(rank=USER_DSH_RANK, source=SOURCE_USER_DSH, path=(isolated_home / "skills").resolve()),
        SkillRoot(rank=100, source="project-dsh", path=(project / ".dsh" / "skills").resolve()),
    ]
    registry = SkillRegistry(SkillConfig(), roots=roots)
    assert registry.roots() == tuple(roots)
    catalog = registry.snapshot()
    assert [item.description for item in catalog.skills] == ["Project copy"]
    assert any("duplicate skill" in error for error in catalog.errors)
