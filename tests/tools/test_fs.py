"""read/write/edit: result formats, pagination, encoding, sandbox enforcement."""

from __future__ import annotations

import pytest

from deeppyro.core.errors import PermissionDenied, ToolError
from deeppyro.sandbox import ApprovalRequest, PermissionGate
from deeppyro.tools import EditTool, ReadTool, WriteTool
from tests.tools.helpers import make_context, write_file


async def test_read_returns_numbered_lines_and_footer(workspace) -> None:  # type: ignore[no-untyped-def]
    path = write_file(workspace, "a.txt", "alpha\nbeta\ngamma\n")
    result = await ReadTool().run({"file_path": "a.txt"}, make_context(workspace))
    assert result.is_error is False
    lines = result.text.splitlines()
    assert lines[0] == f"<path>{path}</path>"
    assert lines[1] == "<type>file</type>"
    assert lines[2] == "<content>"
    assert "1: alpha" in lines
    assert "3: gamma" in lines
    assert lines[-1] == "(End of file - total 3 lines)"
    assert result.meta == {"path": str(path), "totalLines": 3, "offset": 1, "returned": 3}


async def test_read_limit_paginates(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "five.txt", "l1\nl2\nl3\nl4\nl5\n")
    result = await ReadTool().run({"file_path": "five.txt", "limit": 2}, make_context(workspace))
    assert "1: l1" in result.text and "2: l2" in result.text
    assert "3: l3" not in result.text
    assert "(Output capped. Showing lines 1-2. Use offset=3 to continue.)" in result.text


async def test_read_offset_and_limit_window(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "five.txt", "l1\nl2\nl3\nl4\nl5\n")
    result = await ReadTool().run({"file_path": "five.txt", "offset": 2, "limit": 2}, make_context(workspace))
    assert "2: l2" in result.text and "3: l3" in result.text
    assert "1: l1" not in result.text
    assert "Showing lines 2-3 of 5" in result.text


async def test_read_offset_near_eof(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "five.txt", "l1\nl2\nl3\nl4\nl5\n")
    result = await ReadTool().run({"file_path": "five.txt", "offset": 4}, make_context(workspace))
    assert "4: l4" in result.text and "5: l5" in result.text
    assert "l5" in result.text
    assert result.meta["returned"] == 2


async def test_read_rejects_bad_limit_and_offset(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "a.txt", "only\n")
    with pytest.raises(ToolError, match="less than or equal to 2000"):
        await ReadTool().run({"file_path": "a.txt", "limit": 5000}, make_context(workspace))
    with pytest.raises(ToolError, match="beyond the end"):
        await ReadTool().run({"file_path": "a.txt", "offset": 99}, make_context(workspace))
    with pytest.raises(ToolError, match="offset must be an integer"):
        await ReadTool().run({"file_path": "a.txt", "offset": "2"}, make_context(workspace))


async def test_read_missing_file_and_directory(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="not found"):
        await ReadTool().run({"file_path": "nope.txt"}, make_context(workspace))
    (workspace / "dir").mkdir()
    with pytest.raises(ToolError, match="not a regular file"):
        await ReadTool().run({"file_path": "dir"}, make_context(workspace))
    with pytest.raises(ToolError, match="file_path is required"):
        await ReadTool().run({}, make_context(workspace))


async def test_read_empty_file(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "empty.txt", "")
    result = await ReadTool().run({"file_path": "empty.txt"}, make_context(workspace))
    assert result.meta["totalLines"] == 0
    assert "(End of file - total 0 lines)" in result.text


async def test_read_truncates_very_long_lines(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "long.txt", "x" * 2500 + "\n")
    result = await ReadTool().run({"file_path": "long.txt"}, make_context(workspace))
    assert "[line truncated]" in result.text
    assert len(result.text) < 2600


async def test_read_accepts_absolute_path(workspace) -> None:  # type: ignore[no-untyped-def]
    path = write_file(workspace, "abs.txt", "body\n")
    result = await ReadTool().run({"file_path": str(path)}, make_context(workspace))
    assert f"<path>{path}</path>" in result.text


async def test_write_creates_file(workspace) -> None:  # type: ignore[no-untyped-def]
    result = await WriteTool().run(
        {"file_path": "new.txt", "content": "hello\nworld\n"}, make_context(workspace)
    )
    assert result.is_error is False
    path = workspace / "new.txt"
    assert path.read_bytes() == b"hello\nworld\n"
    assert f"<path>{path}</path>" in result.text
    assert "Created file" in result.text
    assert result.meta["created"] is True
    assert result.meta["bytes"] == len(b"hello\nworld\n")


async def test_write_updates_existing_file_and_creates_parents(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "old.txt", "old\n")
    result = await WriteTool().run({"file_path": "old.txt", "content": "new\n"}, make_context(workspace))
    assert "Updated file" in result.text
    assert (workspace / "old.txt").read_bytes() == b"new\n"
    assert result.meta["created"] is False
    nested = await WriteTool().run({"file_path": "a/b/c.txt", "content": "x"}, make_context(workspace))
    assert nested.meta["created"] is True
    assert (workspace / "a" / "b" / "c.txt").read_text() == "x"


async def test_write_requires_content(workspace) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ToolError, match="content is required"):
        await WriteTool().run({"file_path": "a.txt"}, make_context(workspace))
    with pytest.raises(ToolError, match="file_path must be a non-empty string"):
        await WriteTool().run({"file_path": "  ", "content": "x"}, make_context(workspace))


async def test_write_is_denied_in_read_only_mode(workspace) -> None:  # type: ignore[no-untyped-def]
    gate = PermissionGate("read-only", "never", workspace)
    ctx = make_context(workspace, permission=gate)
    with pytest.raises(PermissionDenied, match="read-only"):
        await WriteTool().run({"file_path": "x.txt", "content": "x"}, ctx)
    assert not (workspace / "x.txt").exists()


async def test_write_outside_workspace_requires_approval(tmp_path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    asked: list[ApprovalRequest] = []

    async def grant(req: ApprovalRequest) -> bool:
        asked.append(req)
        return True

    ctx = make_context(workspace, permission=PermissionGate("workspace-write", "ask", workspace, ask=grant))
    outside = tmp_path / "outside.txt"
    result = await WriteTool().run({"file_path": str(outside), "content": "ok"}, ctx)
    assert result.is_error is False
    assert outside.read_text() == "ok"
    assert asked[0].kind == "write-outside-workspace"

    async def deny(req: ApprovalRequest) -> bool:
        return False

    ctx_deny = make_context(
        workspace, permission=PermissionGate("workspace-write", "ask", workspace, ask=deny)
    )
    with pytest.raises(PermissionDenied, match="rejected by the user"):
        await WriteTool().run({"file_path": str(outside), "content": "no"}, ctx_deny)


async def test_write_escalation_requires_justification(tmp_path) -> None:  # type: ignore[no-untyped-def]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = make_context(workspace, permission=PermissionGate("workspace-write", "always", workspace))
    with pytest.raises(ToolError, match="justification is required"):
        await WriteTool().run(
            {"file_path": "x.txt", "content": "x", "sandbox_permissions": "danger-full-access"},
            ctx,
        )
    with pytest.raises(ToolError, match="not wider"):
        await WriteTool().run(
            {
                "file_path": "x.txt",
                "content": "x",
                "sandbox_permissions": "workspace-write",
                "justification": "x",
            },
            ctx,
        )
    result = await WriteTool().run(
        {
            "file_path": "x.txt",
            "content": "x",
            "sandbox_permissions": "danger-full-access",
            "justification": "need full access for this file",
        },
        ctx,
    )
    assert result.is_error is False
    assert (workspace / "x.txt").read_text() == "x"


async def test_edit_replaces_unique_string(workspace) -> None:  # type: ignore[no-untyped-def]
    path = write_file(workspace, "e.txt", "alpha beta gamma\n")
    result = await EditTool().run(
        {"file_path": "e.txt", "old_string": "beta", "new_string": "BETA"},
        make_context(workspace),
    )
    assert result.text == f"The file {path} has been updated successfully."
    assert path.read_text() == "alpha BETA gamma\n"


async def test_edit_replace_all(workspace) -> None:  # type: ignore[no-untyped-def]
    path = write_file(workspace, "e.txt", "x x x\n")
    await EditTool().run(
        {"file_path": "e.txt", "old_string": "x", "new_string": "y", "replace_all": True},
        make_context(workspace),
    )
    assert path.read_text() == "y y y\n"


async def test_edit_requires_unique_match(workspace) -> None:  # type: ignore[no-untyped-def]
    write_file(workspace, "e.txt", "x x\n")
    with pytest.raises(ToolError, match="not unique"):
        await EditTool().run(
            {"file_path": "e.txt", "old_string": "x", "new_string": "y"}, make_context(workspace)
        )
    with pytest.raises(ToolError, match="was not found"):
        await EditTool().run(
            {"file_path": "e.txt", "old_string": "zzz", "new_string": "y"}, make_context(workspace)
        )
    with pytest.raises(ToolError, match="old_string must be a non-empty string"):
        await EditTool().run(
            {"file_path": "e.txt", "old_string": "", "new_string": "y"}, make_context(workspace)
        )
    with pytest.raises(ToolError, match="not found"):
        await EditTool().run(
            {"file_path": "missing.txt", "old_string": "a", "new_string": "b"}, make_context(workspace)
        )


async def test_edit_preserves_bytes_around_replacement(workspace) -> None:  # type: ignore[no-untyped-def]
    path = write_file(workspace, "crlf.txt", "a\r\nb\r\n")
    await EditTool().run(
        {"file_path": "crlf.txt", "old_string": "b", "new_string": "B"}, make_context(workspace)
    )
    assert path.read_bytes() == b"a\r\nB\r\n"
