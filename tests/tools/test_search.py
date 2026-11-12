"""glob and grep: matching rules, ordering, limits, and output layout."""

from __future__ import annotations

import os

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.tools import GlobTool, GrepTool
from tests.tools.helpers import make_context, write_file


async def test_glob_matches_basename_at_any_depth(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "a.txt", "a")
    write_file(workspace, "nested/b.txt", "b")
    write_file(workspace, "nested/deep/c.md", "c")
    result = await GlobTool().run({"pattern": "*.txt"}, make_context(workspace))
    assert result.is_error is False
    normalized = result.text.replace("\\", "/")
    assert "a.txt" in normalized
    assert "nested/b.txt" in normalized
    assert "c.md" not in normalized
    assert result.meta["count"] == 2


async def test_glob_pattern_with_separator(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "nested/deep/x.txt", "x")
    write_file(workspace, "top.txt", "t")
    result = await GlobTool().run({"pattern": "nested/**/*.txt"}, make_context(workspace))
    normalized = result.text.replace("\\", "/")
    assert "nested/deep/x.txt" in normalized
    assert "top.txt" not in normalized


async def test_glob_empty_result(workspace) -> None:  # type: ignore[no-untyped-def]
    result = await GlobTool().run({"pattern": "*.zzz"}, make_context(workspace))
    assert result.text == "No files found"
    assert result.meta["count"] == 0


async def test_glob_orders_by_mtime_descending(workspace) -> None:  # type: ignore[no-untyped-def]
    older = write_file(workspace, "older.txt", "old")
    newer = write_file(workspace, "newer.txt", "new")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_000_100, 1_700_000_100))
    result = await GlobTool().run({"pattern": "*.txt"}, make_context(workspace))
    lines = result.text.splitlines()
    assert lines.index("newer.txt") < lines.index("older.txt")


async def test_glob_skips_vcs_metadata(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, ".git/config.txt", "vcs")
    write_file(workspace, "real.txt", "real")
    result = await GlobTool().run({"pattern": "*.txt"}, make_context(workspace))
    assert "real.txt" in result.text
    assert ".git" not in result.text


async def test_glob_respects_max_results(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "a.txt", "a")
    write_file(workspace, "b.txt", "b")
    result = await GlobTool(max_results=1).run({"pattern": "*.txt"}, make_context(workspace))
    assert result.meta == {"count": 2, "returned": 1}
    assert "(Showing the first 1 of 2 files. Refine the pattern for more.)" in result.text


async def test_glob_validates_pattern_and_path(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="pattern is required"):
        await GlobTool().run({}, make_context(workspace))
    with pytest.raises(ToolError, match="absolute"):
        await GlobTool().run({"pattern": str(workspace / "*.txt")}, make_context(workspace))
    with pytest.raises(ToolError, match="does not exist"):
        await GlobTool().run({"pattern": "*.txt", "path": "missing"}, make_context(workspace))


async def test_glob_searches_explicit_path(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "sub/deep.txt", "x")
    write_file(workspace, "top.txt", "t")
    result = await GlobTool().run({"pattern": "*.txt", "path": "sub"}, make_context(workspace))
    assert "deep.txt" in result.text
    assert "top.txt" not in result.text


async def test_grep_groups_matches_by_file(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "a.py", "import os\nprint('CUDA')\n")
    write_file(workspace, "b.txt", "CUDA\n")
    result = await GrepTool().run({"pattern": "CUDA"}, make_context(workspace))
    assert "Found 2 matches" in result.text
    assert "a.py" in result.text and "b.txt" in result.text
    assert "Line 2: print('CUDA')" in result.text
    assert "Line 1: CUDA" in result.text
    assert result.meta["matches"] == 2
    assert result.meta["files"] == 2


async def test_grep_include_filters_files(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "a.py", "needle\n")
    write_file(workspace, "b.txt", "needle\n")
    result = await GrepTool().run({"pattern": "needle", "include": "*.py"}, make_context(workspace))
    assert "a.py" in result.text
    assert "b.txt" not in result.text
    assert result.meta["matches"] == 1


async def test_grep_empty_and_invalid_pattern(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "a.txt", "hello\n")
    result = await GrepTool().run({"pattern": "absent"}, make_context(workspace))
    assert result.text == "No matches found"
    with pytest.raises(ToolError, match="invalid regular expression"):
        await GrepTool().run({"pattern": "("}, make_context(workspace))


async def test_grep_rejects_include_lists(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="single glob"):
        await GrepTool().run({"pattern": "x", "include": "*.py,*.ts"}, make_context(workspace))
    with pytest.raises(ToolError, match="single glob"):
        await GrepTool().run({"pattern": "x", "include": "!*.py"}, make_context(workspace))


async def test_grep_max_matches_reports_total(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "many.txt", "hit\nhit\nhit\nhit\nhit\n")
    result = await GrepTool(max_matches=2).run({"pattern": "hit"}, make_context(workspace))
    assert "Found 2 of 5 matches" in result.text
    assert "(Showing the first 2 of 5 matches. Refine the pattern for more.)" in result.text
    assert result.meta == {"matches": 5, "returned": 2, "files": 1}


async def test_grep_skips_vcs_and_binary_files(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, ".git/HEAD", "needle\n")
    (workspace / "blob.bin").write_bytes(b"needle\x00binary")
    write_file(workspace, "real.txt", "needle\n")
    result = await GrepTool().run({"pattern": "needle"}, make_context(workspace))
    assert "real.txt" in result.text
    assert ".git" not in result.text
    assert "blob.bin" not in result.text


async def test_grep_single_file_path(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "one.txt", "alpha\nbeta\n")
    result = await GrepTool().run({"pattern": "beta", "path": "one.txt"}, make_context(workspace))
    assert "Line 2: beta" in result.text
    assert result.meta["matches"] == 1


async def test_grep_outside_cwd_uses_absolute_group_header(tmp_path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    target = other / "outside.txt"
    target.write_text("needle\n", encoding="utf-8", newline="")
    result = await GrepTool().run({"pattern": "needle", "path": str(target)}, make_context(workspace))
    assert str(target) in result.text
