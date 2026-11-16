"""Exact-version exemptions and plugin.toml rewriting."""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from deeppyro import paths
from deeppyro.plugins.install_errors import InstallFailure, PreflightFailure, UnknownPlugin
from deeppyro.plugins.install_requirements import validate_version
from deeppyro.plugins.metadata import MetadataError, load_metadata


@dataclass(frozen=True, slots=True)
class AllowVersionReport:
    plugin_id: str
    version: str
    changed: bool


_EXEMPT_KEYS = ("dpyro_exempt_versions", "exempt_versions")

_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*=\s*(.*)$")


def _table_header(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[[") or not stripped.endswith("]"):
        return None
    return stripped[1:-1].strip()


def _compat_bounds(lines: list[str]) -> tuple[int, int] | None:
    start: int | None = None
    for index, line in enumerate(lines):
        name = _table_header(line)
        if name is None:
            continue
        if start is not None:
            return start, index
        if name == "compat":
            start = index
    if start is None:
        return None
    return start, len(lines)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _quote(value: str, *, single: bool) -> str:
    return f"'{value}'" if single else json.dumps(value)


def _array_text(values: Iterable[str], *, indent: str, single: bool, multiline: bool) -> str:
    rendered = [_quote(value, single=single) for value in values]
    if not multiline:
        return "[" + ", ".join(rendered) + "]"
    body = "".join(f"{indent}    {item},\n" for item in rendered)
    return "[\n" + body + indent + "]"


def _bracket_depth(block: Iterable[str]) -> int:
    text = " ".join(part.strip() for part in block)
    return text.count("[") - text.count("]")


def _array_values(block: Iterable[str]) -> list[str]:
    text = " ".join(part.strip() for part in block)
    start = text.find("[")
    end = text.rfind("]")
    inner = text[start + 1 : end] if start >= 0 and end > start else ""
    return re.findall(r"""["']([^"']+)["']""", inner)


def _uses_single_quotes(block: Iterable[str]) -> bool:
    text = " ".join(block)
    return text.count("'") > text.count('"')


def _section_indent(lines: list[str], start: int, end: int) -> str:
    for index in range(start + 1, end):
        match = _KEY_RE.match(lines[index])
        if match is not None and match.group(1):
            return match.group(1)
    return ""


def with_exempt_version(text: str, version: str) -> str:
    """Add ``version`` to ``[compat].dpyro_exempt_versions`` keeping the style."""
    newline = _newline(text)
    had_trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    bounds = _compat_bounds(lines)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(("[compat]", f"dpyro_exempt_versions = {json.dumps([version])}"))
        return newline.join(lines) + (newline if had_trailing else "")
    start, end = bounds
    key_index: int | None = None
    for index in range(start + 1, end):
        match = _KEY_RE.match(lines[index])
        if match is not None and match.group(2) in _EXEMPT_KEYS:
            key_index = index
            break
    if key_index is None:
        indent = _section_indent(lines, start, end)
        lines.insert(start + 1, f"{indent}dpyro_exempt_versions = {json.dumps([version])}")
        return newline.join(lines) + (newline if had_trailing else "")
    match = _KEY_RE.match(lines[key_index])
    assert match is not None
    indent, key, raw = match.group(1), match.group(2), match.group(3)
    block = [raw]
    consumed = 1
    while _bracket_depth(block) > 0 and key_index + consumed < end:
        block.append(lines[key_index + consumed])
        consumed += 1
    values = _array_values(block)
    if version in values:
        return text
    values.append(version)
    multiline = consumed > 1
    rendered = _array_text(values, indent=indent, single=_uses_single_quotes(block), multiline=multiline)
    replacement = f"{indent}{key} = {rendered}"
    lines[key_index : key_index + consumed] = replacement.splitlines()
    return newline.join(lines) + (newline if had_trailing else "")


def _plugin_toml(plugin_id: str, root: Path) -> Path:
    path = root / plugin_id / "plugin.toml"
    if not path.is_file():
        raise UnknownPlugin(plugin_id)
    return path


def allow_version(plugin_id: str, version: str, *, root: Path | None = None) -> AllowVersionReport:
    """Persist one exact version in ``[compat].dpyro_exempt_versions``."""
    plugin_root = Path(root) if root is not None else paths.plugins_dir()
    toml_path = _plugin_toml(plugin_id, plugin_root)
    problem = validate_version(version)
    if problem is not None:
        raise PreflightFailure((problem,))
    original = toml_path.read_text(encoding="utf-8")
    updated = with_exempt_version(original, version)
    if updated == original:
        return AllowVersionReport(plugin_id, version, False)
    backup = toml_path.with_name("plugin.toml.bak")
    try:
        backup.write_text(original, encoding="utf-8")
        toml_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise InstallFailure(f"could not write {toml_path}: {exc}", rolled_back=False) from exc
    try:
        load_metadata(toml_path, plugin_id=plugin_id)
    except MetadataError as exc:
        with contextlib.suppress(OSError):
            toml_path.write_text(original, encoding="utf-8")
        raise PreflightFailure((f"{exc.kind}: {exc.message}",)) from exc
    return AllowVersionReport(plugin_id, version, True)
