"""Unit tests for tool-call summaries, diffs and result cleanup."""

from __future__ import annotations

import json

from deeppyro.tui.widgets.tool_card import (
    clean_result_text,
    diff_preview_lines,
    parse_arguments,
    summarize_arguments,
)


def test_parse_arguments_accepts_json_and_python_repr() -> None:
    assert parse_arguments('{"path": "a.py"}') == {"path": "a.py"}
    assert parse_arguments("{'path': 'b.py'}") == {"path": "b.py"}
    assert parse_arguments({"path": "c.py"}) == {"path": "c.py"}
    assert parse_arguments("not json") == {}
    assert parse_arguments("") == {}
    assert parse_arguments(None) == {}


def test_summarize_path_tools_return_path() -> None:
    assert summarize_arguments("read", '{"path": "src/a.py", "offset": 1}') == "src/a.py"
    assert summarize_arguments("edit", '{"file_path": "src/b.py"}') == "src/b.py"


def test_summarize_shell_tools_return_command_prefix() -> None:
    command = "uv run pytest tests/tui -q --maxfail=1"
    summary = summarize_arguments("pwsh", json.dumps({"command": command}))
    assert summary.startswith("uv run pytest")
    long_command = "echo " + "x" * 200
    assert len(summarize_arguments("bash", json.dumps({"command": long_command}))) <= 60


def test_summarize_unknown_tool_falls_back_to_json() -> None:
    summary = summarize_arguments("custom", '{"alpha": 1, "beta": 2}')
    assert "alpha" in summary and "beta" in summary


def test_summarize_unparseable_arguments_is_truncated_raw_text() -> None:
    summary = summarize_arguments("custom", "raw payload " + "y" * 200)
    assert summary.startswith("raw payload")
    assert len(summary) <= 80


def test_diff_edit_old_new() -> None:
    arguments = json.dumps({"path": "a.py", "old": "x = 1\ny = 2", "new": "x = 1\ny = 3"})
    lines = diff_preview_lines("edit", arguments)
    assert ("-", "x = 1") in lines
    assert ("-", "y = 2") in lines
    assert ("+", "y = 3") in lines


def test_diff_edit_snake_case_keys() -> None:
    arguments = json.dumps({"old_string": "before", "new_string": "after"})
    assert diff_preview_lines("edit", arguments) == [("-", "before"), ("+", "after")]


def test_diff_write_content_is_added() -> None:
    arguments = json.dumps({"path": "a.py", "content": "first\nsecond"})
    assert diff_preview_lines("write", arguments) == [("+", "first"), ("+", "second")]


def test_diff_is_empty_for_read_or_unparseable() -> None:
    assert diff_preview_lines("read", '{"path": "a.py"}') == []
    assert diff_preview_lines("edit", "not json") == []


def test_clean_result_text_strips_content_wrapper() -> None:
    assert clean_result_text("<content>1: hi</content>") == "1: hi"
    assert clean_result_text("  <output>ok</output>  ") == "ok"
    assert clean_result_text("plain") == "plain"
    assert clean_result_text("<content>unbalanced") == "<content>unbalanced"


def test_clean_result_text_keeps_only_filesystem_payload() -> None:
    read_result = (
        "<path>D:/repo/README.md</path>\n<type>file</type>\n"
        "<content>\n1: # DeepPyro\n2: python\n</content>\n(End of file - total 2 lines)"
    )
    assert clean_result_text(read_result) == "1: # DeepPyro\n2: python"
    write_result = "<path>D:/repo/a.py</path>\n<type>file</type>\n<content>\nUpdated file\n</content>"
    assert clean_result_text(write_result) == "Updated file"


def test_clean_result_text_keeps_unknown_path_frame() -> None:
    framed = "<path>D:/repo/a.py</path>\n<type>file</type>"
    assert clean_result_text(framed) == framed
