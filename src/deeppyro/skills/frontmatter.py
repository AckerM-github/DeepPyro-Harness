"""Frontmatter parsing for SKILL.md files.

Skill files use a deliberately small YAML subset: scalar key/value pairs
between two --- delimiter lines. Multi-line values, nested mappings and lists
are rejected with a clear parse error; discovery records the error and skips
the file instead of loading surprising instructions.

Missing name/description fall back to the directory name and the first body
paragraph. That keeps a bare SKILL.md usable while still validating every name
against the public kebab-case grammar.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import SkillParseError

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_DELIMITER = "---"

_OLD_KEYS = {
    "disableModelInvocation": "disable-model-invocation",
    "modelInvocable": "user-invocable / disable-model-invocation",
    "userInvocable": "user-invocable",
}
_TRUE_VALUES = {"true", "yes", "on", "1"}
_FALSE_VALUES = {"false", "no", "off", "0"}


@dataclass(frozen=True, slots=True)
class SkillFile:
    """One parsed skill file, before it becomes a SkillDefinition."""

    name: str
    description: str
    when_to_use: str = ""
    model_invocable: bool = True
    user_invocable: bool = True
    body: str = ""


def is_valid_skill_name(name: str) -> bool:
    """Return True when name matches the public kebab-case grammar."""
    return isinstance(name, str) and bool(SKILL_NAME_RE.fullmatch(name))


def fallback_skill_name(path: Path) -> str:
    """Derive a fallback name from the skill directory or file stem."""
    candidate = Path(path)
    if candidate.name.lower() == "skill.md":
        return candidate.parent.name
    return candidate.stem


def parse_skill_file(path: Path, *, fallback_name: str | None = None) -> SkillFile:
    """Parse one SKILL.md (or flat Markdown) file.

    Raises SkillParseError for unreadable files and invalid content.
    """
    candidate = Path(path)
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SkillParseError(f"cannot read skill file {candidate}: {exc}") from exc
    name = fallback_name if fallback_name is not None else fallback_skill_name(candidate)
    return parse_skill_text(text, source=str(candidate), fallback_name=name)


def parse_skill_text(text: str, *, source: str = "<skill>", fallback_name: str = "") -> SkillFile:
    """Parse skill text with an explicit source label for diagnostics."""
    raw = text.lstrip("\ufeff")
    fields, body = _split_frontmatter(raw, source)
    name = _string_field(fields, "name") or fallback_name.strip()
    if not name:
        raise SkillParseError(f"{source}: skill name is missing and no directory fallback is available")
    if not is_valid_skill_name(name):
        raise SkillParseError(f"{source}: invalid skill name {name!r}; skill names must be kebab-case")
    description = _string_field(fields, "description") or first_paragraph(body)
    when_to_use = _string_field(fields, "whenToUse") or _string_field(fields, "when_to_use")
    disable = fields.get("disable-model-invocation")
    model_invocable = True if disable is None else not _boolean(disable, "disable-model-invocation", source)
    user = fields.get("user-invocable")
    user_invocable = True if user is None else _boolean(user, "user-invocable", source)
    return SkillFile(
        name=name,
        description=description.strip(),
        when_to_use=when_to_use.strip(),
        model_invocable=model_invocable,
        user_invocable=user_invocable,
        body=body.strip(),
    )


def first_paragraph(body: str) -> str:
    """Return the first non-empty paragraph, with heading markers removed."""
    collected: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            if collected:
                break
            continue
        collected.append(line)
    text = " ".join(collected).strip()
    return text.lstrip("#").strip()


def _split_frontmatter(text: str, source: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index >= len(lines) or lines[index].strip() != FRONTMATTER_DELIMITER:
        return {}, text
    end: int | None = None
    for position in range(index + 1, len(lines)):
        if lines[position].strip() == FRONTMATTER_DELIMITER:
            end = position
            break
    if end is None:
        raise SkillParseError(f"{source}: frontmatter has no closing {FRONTMATTER_DELIMITER} delimiter line")
    fields = _parse_fields(lines[index + 1 : end], source)
    body = "\n".join(lines[end + 1 :])
    return fields, body


def _parse_fields(lines: list[str], source: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for offset, line in enumerate(lines):
        number = offset + 2
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            raise SkillParseError(f"{source}: line {number}: multi-line frontmatter values are not supported")
        key, separator, value = line.partition(":")
        if not separator:
            raise SkillParseError(f"{source}: line {number}: expected a key: value pair")
        key = key.strip()
        if not key:
            raise SkillParseError(f"{source}: line {number}: frontmatter key must not be empty")
        if key in _OLD_KEYS:
            raise SkillParseError(
                f"{source}: frontmatter key {key!r} is not supported; use {_OLD_KEYS[key]!r} instead"
            )
        if key in fields:
            raise SkillParseError(f"{source}: line {number}: duplicate frontmatter key {key!r}")
        fields[key] = _scalar(value.strip(), source, number)
    return fields


def _scalar(raw: str, source: str, number: int) -> str:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        try:
            return str(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise SkillParseError(f"{source}: line {number}: invalid quoted string: {exc}") from exc
    if len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def _string_field(fields: dict[str, str], key: str) -> str:
    value = fields.get(key)
    return value.strip() if isinstance(value, str) else ""


def _boolean(raw: str, key: str, source: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise SkillParseError(f"{source}: frontmatter key {key!r} must be a boolean")


__all__ = [
    "FRONTMATTER_DELIMITER",
    "SKILL_NAME_RE",
    "SkillFile",
    "fallback_skill_name",
    "first_paragraph",
    "is_valid_skill_name",
    "parse_skill_file",
    "parse_skill_text",
]
