"""Frontmatter parsing semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from deeppyro.skills.errors import SkillParseError
from deeppyro.skills.frontmatter import (
    fallback_skill_name,
    is_valid_skill_name,
    parse_skill_file,
    parse_skill_text,
)

from .helpers import write_skill


def test_parses_canonical_fields_and_trims_body() -> None:
    parsed = parse_skill_text(
        "---\n"
        "name: demo-skill\n"
        "description: Does demo things\n"
        "whenToUse: when a demo is needed\n"
        "---\n"
        "\n"
        "  Follow these steps.  \n",
        source="demo.md",
    )
    assert parsed.name == "demo-skill"
    assert parsed.description == "Does demo things"
    assert parsed.when_to_use == "when a demo is needed"
    assert parsed.model_invocable is True
    assert parsed.user_invocable is True
    assert parsed.body == "Follow these steps."


def test_missing_name_falls_back_to_directory_and_description_to_first_paragraph(tmp_path: Path) -> None:
    path = write_skill(
        tmp_path,
        "fallback",
        frontmatter=False,
        body="# Heading\n\nFirst paragraph line one.\nLine two.\n\nLater text.",
    )
    parsed = parse_skill_file(path)
    assert parsed.name == "fallback"
    assert parsed.description == "Heading"


def test_flat_file_uses_stem_and_strips_heading_marker(tmp_path: Path) -> None:
    path = tmp_path / "loose.md"
    path.write_text("# A loose skill\n\nBody text.", encoding="utf-8")
    parsed = parse_skill_file(path)
    assert parsed.name == "loose"
    assert parsed.description == "A loose skill"


def test_quoted_scalars_are_unwrapped() -> None:
    parsed = parse_skill_text(
        '---\nname: "quoted"\ndescription: "A: colon inside"\n---\nbody\n',
        source="quoted.md",
    )
    assert parsed.name == "quoted"
    assert parsed.description == "A: colon inside"
    single = parse_skill_text(
        "---\nname: single\ndescription: 'It''s fine'\n---\nbody\n",
        source="single.md",
    )
    assert single.description == "It's fine"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", False),
        ("yes", False),
        ("on", False),
        ("1", False),
        ("false", True),
        ("no", True),
        ("off", True),
        ("0", True),
    ],
)
def test_disable_model_invocation_boolean_variants(raw: str, expected: bool) -> None:
    parsed = parse_skill_text(
        "---\nname: demo\ndisable-model-invocation: " + raw + "\n---\nbody\n",
        source="demo.md",
    )
    assert parsed.model_invocable is expected


@pytest.mark.parametrize("raw,expected", [("true", True), ("yes", True), ("false", False), ("no", False)])
def test_user_invocable_boolean_variants(raw: str, expected: bool) -> None:
    parsed = parse_skill_text(
        "---\nname: demo\nuser-invocable: " + raw + "\n---\nbody\n",
        source="demo.md",
    )
    assert parsed.user_invocable is expected


def test_missing_closing_delimiter_is_an_error() -> None:
    with pytest.raises(SkillParseError, match="no closing"):
        parse_skill_text("---\nname: demo\ndescription: x\n", source="missing.md")


def test_multiline_and_nested_frontmatter_are_rejected() -> None:
    with pytest.raises(SkillParseError, match="multi-line"):
        parse_skill_text("---\nname: demo\ndescription: |\n  nested\n---\nbody\n", source="nested.md")


@pytest.mark.parametrize("key", ["disableModelInvocation", "userInvocable", "modelInvocable"])
def test_old_camel_case_keys_are_rejected(key: str) -> None:
    with pytest.raises(SkillParseError, match="not supported"):
        parse_skill_text("---\nname: demo\n" + key + ": true\n---\nbody\n", source="old.md")


def test_duplicate_key_and_missing_colon_are_errors() -> None:
    with pytest.raises(SkillParseError, match="duplicate"):
        parse_skill_text("---\nname: demo\nname: other\n---\nbody\n", source="dup.md")
    with pytest.raises(SkillParseError, match="key: value"):
        parse_skill_text("---\nname: demo\nnot a pair\n---\nbody\n", source="pair.md")


def test_invalid_name_and_invalid_boolean_are_errors() -> None:
    with pytest.raises(SkillParseError, match="invalid skill name"):
        parse_skill_text("---\nname: Bad Name\n---\nbody\n", source="bad.md")
    with pytest.raises(SkillParseError, match="must be a boolean"):
        parse_skill_text("---\nname: demo\nuser-invocable: maybe\n---\nbody\n", source="bool.md")


def test_unreadable_file_raises_parse_error(tmp_path: Path) -> None:
    with pytest.raises(SkillParseError, match="cannot read"):
        parse_skill_file(tmp_path / "missing" / "SKILL.md")


def test_name_helpers() -> None:
    assert is_valid_skill_name("demo-skill-2")
    assert not is_valid_skill_name("Demo")
    assert not is_valid_skill_name("demo_skill")
    assert fallback_skill_name(Path("/x/my-skill/SKILL.md")) == "my-skill"
    assert fallback_skill_name(Path("/x/flat.md")) == "flat"


def test_crlf_frontmatter_is_supported() -> None:
    parsed = parse_skill_text(
        "---\r\nname: crlf\r\ndescription: windows\r\n---\r\nbody\r\n", source="crlf.md"
    )
    assert parsed.name == "crlf"
    assert parsed.description == "windows"
    assert parsed.body == "body"
