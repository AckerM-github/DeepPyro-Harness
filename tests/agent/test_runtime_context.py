"""Tests for runtime-context snapshot assembly."""

from __future__ import annotations

from pathlib import Path

from deeppyro.prompt import PromptContext
from deeppyro.prompt.runtime_context import (
    RUNTIME_CONTEXT_HEADER,
    render_runtime_context,
    runtime_context_sections,
)


def make_context(tmp_path: Path, **overrides: object) -> PromptContext:
    defaults: dict[str, object] = {
        "cwd": tmp_path,
        "platform": "win32",
        "session_id": "session-runtime",
        "started_at": 1_700_000_000_000,
        "permission_preset": "workspace-write",
        "approval_policy": "ask",
        "model": "deepseek-v4-pro",
    }
    defaults.update(overrides)
    return PromptContext(**defaults)  # type: ignore[arg-type]


def test_runtime_context_sections_include_runtime_facts(tmp_path: Path) -> None:
    sections = runtime_context_sections(make_context(tmp_path))
    assert [section["name"] for section in sections] == ["runtime"]
    text = sections[0]["text"]
    assert str(tmp_path) in text
    assert "win32" in text
    assert "deepseek-v4-pro" in text
    assert "session-runtime" in text
    assert "workspace-write" in text
    assert "ask" in text


def test_render_runtime_context_prefixes_snapshot_header(tmp_path: Path) -> None:
    sections = runtime_context_sections(make_context(tmp_path))
    rendered = render_runtime_context(sections)
    assert rendered.startswith(RUNTIME_CONTEXT_HEADER)
    assert str(tmp_path) in rendered


def test_render_runtime_context_skips_empty_sections() -> None:
    rendered = render_runtime_context([{"name": "empty", "text": "   "}])
    assert rendered == ""


def test_render_runtime_context_joins_sections_in_order() -> None:
    rendered = render_runtime_context([{"name": "a", "text": "first"}, {"name": "b", "text": "second"}])
    assert rendered == RUNTIME_CONTEXT_HEADER + "\n\nfirst\n\nsecond"
