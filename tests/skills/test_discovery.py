"""Filesystem discovery: roots, ranking and scan behavior."""

from __future__ import annotations

from pathlib import Path

from deeppyro.skills.discovery import discover_roots, find_project_root, scan_skills
from deeppyro.skills.types import (
    BUNDLED_RANK,
    CUSTOM_RANK,
    PROJECT_AGENTS_RANK,
    PROJECT_DSH_RANK,
    SOURCE_BUNDLED,
    SOURCE_CUSTOM,
    SOURCE_PROJECT_AGENTS,
    SOURCE_PROJECT_DSH,
    SOURCE_USER_DSH,
    USER_DSH_RANK,
    SkillConfig,
)

from .helpers import make_project, write_flat, write_skill


def test_discover_roots_order_and_sources(tmp_path: Path, isolated_home: Path) -> None:
    project = make_project(tmp_path)
    custom = tmp_path / "custom-skills"
    bundled = tmp_path / "bundled-skills"
    config = SkillConfig(
        custom_skill_dirs=[str(custom)],
        bundled_skill_dir=str(bundled),
    )
    roots = discover_roots(project, config)
    assert [item.rank for item in roots] == [
        PROJECT_DSH_RANK,
        PROJECT_AGENTS_RANK,
        CUSTOM_RANK,
        USER_DSH_RANK,
        BUNDLED_RANK,
    ]
    assert [item.source for item in roots] == [
        SOURCE_PROJECT_DSH,
        SOURCE_PROJECT_AGENTS,
        SOURCE_CUSTOM,
        SOURCE_USER_DSH,
        SOURCE_BUNDLED,
    ]
    assert roots[0].path == project / ".dsh" / "skills"
    assert roots[1].path == project / ".agents" / "skills"
    assert roots[2].path == custom.resolve()
    assert roots[3].path == (isolated_home / "skills").resolve()
    assert roots[4].path == bundled.resolve()


def test_discover_roots_resolves_relative_custom_dir_against_project(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    config = SkillConfig(custom_skill_dirs=["shared/skills"])
    roots = discover_roots(project, config)
    custom = next(item for item in roots if item.source == SOURCE_CUSTOM)
    assert custom.path == (project / "shared" / "skills").resolve()


def test_discover_roots_dedupes_resolved_paths(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    shared = project / ".dsh" / "skills"
    config = SkillConfig(custom_skill_dirs=[str(shared), str(shared)])
    roots = discover_roots(project, config)
    matching = [item for item in roots if item.path == shared.resolve()]
    assert len(matching) == 1
    assert matching[0].source == SOURCE_PROJECT_DSH


def test_discover_roots_can_disable_default_project_roots(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    config = SkillConfig(include_default_roots=False)
    roots = discover_roots(project, config)
    sources = [item.source for item in roots]
    assert SOURCE_PROJECT_DSH not in sources
    assert SOURCE_PROJECT_AGENTS not in sources
    assert SOURCE_USER_DSH in sources


def test_find_project_root_walks_up_to_git_marker(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    nested = project / "src" / "pkg"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == project.resolve()


def test_find_project_root_returns_cwd_without_git(tmp_path: Path) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    assert find_project_root(loose) == loose.resolve()


def test_scan_finds_directory_and_flat_skills_and_skips_ignored_entries(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    write_skill(skills, "zeta", description="Zeta skill")
    write_skill(skills, "alpha", description="Alpha skill")
    write_flat(skills, "flat.md", "---\nname: flat\ndescription: Flat skill\n---\nbody\n")
    (skills / ".hidden").mkdir()
    (skills / ".hidden" / "SKILL.md").write_text("---\nname: hidden\n---\n", encoding="utf-8")
    (skills / "no-skill-md").mkdir()
    (skills / "no-skill-md" / "README.md").write_text("nope", encoding="utf-8")
    (skills / "notes.txt").write_text("not a skill", encoding="utf-8")

    result = scan_skills([make_root(skills)])
    assert [item.name for item in result.skills] == ["alpha", "flat", "zeta"]
    assert result.errors == ()


def test_scan_summary_fields(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    path = write_skill(
        skills,
        "demo",
        description="Demo description",
        extra="whenToUse: for demos\ndisable-model-invocation: true\n",
    )
    result = scan_skills([make_root(skills)])
    assert len(result.skills) == 1
    summary = result.skills[0]
    assert summary.name == "demo"
    assert summary.description == "Demo description"
    assert summary.when_to_use == "for demos"
    assert summary.model_invocable is False
    assert summary.source == SOURCE_CUSTOM
    assert summary.provider == "filesystem"
    assert summary.path == str(path)
    assert summary.resource_base == str(path.parent)


def test_scan_records_parse_errors_and_continues(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    write_skill(skills, "good", description="Good")
    broken = skills / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: Bad Name\n---\nbody\n", encoding="utf-8")

    result = scan_skills([make_root(skills)])
    assert [item.name for item in result.skills] == ["good"]
    assert len(result.errors) == 1
    assert "invalid skill name" in result.errors[0]


def test_scan_lower_rank_wins_and_duplicate_is_reported(tmp_path: Path, isolated_home: Path) -> None:
    project = make_project(tmp_path)
    write_skill(project / ".dsh" / "skills", "shared", description="Project copy")
    write_skill(isolated_home / "skills", "shared", description="User copy")

    result = scan_skills(discover_roots(project, SkillConfig(include_default_roots=True)))
    assert len(result.skills) == 1
    assert result.skills[0].description == "Project copy"
    assert result.skills[0].source == SOURCE_PROJECT_DSH
    assert any("duplicate skill 'shared'" in error for error in result.errors)


def test_scan_missing_roots_are_silent(tmp_path: Path) -> None:
    result = scan_skills([make_root(tmp_path / "does-not-exist")])
    assert result.skills == ()
    assert result.errors == ()


def make_root(path: Path):
    from deeppyro.skills.discovery import SkillRoot

    return SkillRoot(rank=CUSTOM_RANK, source=SOURCE_CUSTOM, path=Path(path).resolve())
