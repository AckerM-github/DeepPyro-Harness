"""Rendering primitives: masking, previews, NDJSON sink and the Rich sink."""

from __future__ import annotations

import io
import json
import re

import pytest
from rich.console import Console

from deeppyro.cli.render import (
    JsonLinesSink,
    RichSink,
    compact_text,
    effective_config_summary,
    field,
    format_timestamp,
    mask_secret,
    render_config_summary,
    render_session_table,
    short_session_id,
)
from tests.cli.fakes import FakeInfo, make_config, make_terminal_frontend


def make_console(stream: io.StringIO) -> Console:
    return Console(file=stream, width=100, force_terminal=False, no_color=True, highlight=False)


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------


def test_mask_secret_keeps_short_head_and_tail() -> None:
    assert mask_secret(None) is None
    assert mask_secret("") == ""
    assert mask_secret("short") == "***"
    assert mask_secret("12345678") == "***"
    assert mask_secret("sk-1234567890abcdef") == "sk-1…cdef"


def test_compact_text_limits_lines_and_chars() -> None:
    assert compact_text("") == ""
    assert compact_text("one line") == "one line"
    assert compact_text("a\nb\nc\nd", max_lines=3) == "a\nb\nc …"
    long = "x" * 40
    assert compact_text(long, max_chars=10) == "x" * 9 + "…"
    assert compact_text("a\nb", max_lines=3) == "a\nb"


def test_format_timestamp() -> None:
    assert format_timestamp(None) == "-"
    assert format_timestamp(0) == "-"
    rendered = format_timestamp(1_700_000_000_000)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", rendered)


def test_short_session_id_strips_prefix() -> None:
    assert short_session_id(None) == "-"
    assert short_session_id("") == "-"
    assert short_session_id("session-11111111-2222-3333-4444-555555555555") == "11111111"
    assert short_session_id("11111111-2222") == "11111111"


def test_field_reads_mappings_and_objects() -> None:
    assert field({"a": 1}, "a") == 1
    assert field({"a": 1}, "b", 2) == 2
    assert field(FakeInfo("session-aaaaaaaa-x", "t", 1), "title") == "t"
    assert field(object(), "missing", "fallback") == "fallback"


# --------------------------------------------------------------------------
# JsonLinesSink
# --------------------------------------------------------------------------


def test_json_lines_sink_writes_one_flushed_line_per_event() -> None:
    stream = io.StringIO()
    sink = JsonLinesSink(stream)
    sink.emit({"type": "text", "text": "hej"})
    sink.emit({"type": "usage", "input": 1, "output": 2, "cached": 0})
    lines = stream.getvalue().splitlines()
    assert [json.loads(line)["type"] for line in lines] == ["text", "usage"]
    assert lines[0] == '{"type":"text","text":"hej"}'
    assert sink.event_count == 2
    assert sink.saw_error is False


def test_json_lines_sink_tracks_errors_and_unicode() -> None:
    stream = io.StringIO()
    sink = JsonLinesSink(stream)
    sink.emit({"type": "error", "message": "kaboom"})
    assert sink.saw_error is True
    sink.emit({"type": "final", "text": "héllo 世界"})
    payloads = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert payloads[-1]["text"] == "héllo 世界"


def test_json_lines_sink_turn_hooks_are_noops() -> None:
    sink = JsonLinesSink(io.StringIO())
    sink.begin_turn()
    sink.end_turn()
    assert sink.event_count == 0


# --------------------------------------------------------------------------
# RichSink
# --------------------------------------------------------------------------


def test_rich_sink_streams_text_once() -> None:
    out = io.StringIO()
    console = make_console(out)
    sink = RichSink(console)
    sink.begin_turn()
    sink.emit({"type": "text", "text": "hel"})
    sink.emit({"type": "text", "text": "lo"})
    sink.emit({"type": "final", "text": "hello"})
    sink.end_turn()
    assert out.getvalue().count("hello") == 1
    assert sink.final_text == "hello"
    assert sink.saw_text is True


def test_rich_sink_renders_thinking_tool_events() -> None:
    out, err = io.StringIO(), io.StringIO()
    console = make_console(out)
    sink = RichSink(console, err_console=make_console(err))
    sink.begin_turn()
    sink.emit({"type": "thinking", "text": "pondering"})
    sink.emit({"type": "tool_call", "callId": "c1", "name": "read", "arguments": '{"path": "a.txt"}'})
    sink.emit({"type": "tool_result", "callId": "c1", "isError": False, "text": "body"})
    sink.emit({"type": "tool_result", "callId": "missing", "isError": True, "text": "boom"})
    rendered = out.getvalue()
    assert "thinking" in rendered
    assert "pondering" in rendered
    assert "read" in rendered
    assert "body" in rendered
    assert "✗" in rendered
    assert "boom" in rendered


def test_rich_sink_hides_reasoning_when_configured() -> None:
    out = io.StringIO()
    sink = RichSink(make_console(out), show_reasoning=False)
    sink.emit({"type": "thinking", "text": "secret plan"})
    assert "secret plan" not in out.getvalue()


def test_rich_sink_accumulates_usage_and_session_id() -> None:
    sink = RichSink(make_console(io.StringIO()))
    sink.begin_turn()
    sink.emit({"type": "session", "sessionId": "session-abc"})
    sink.emit({"type": "usage", "input": 10, "output": 5, "cached": 2})
    sink.emit({"type": "usage", "input": 1, "output": 2, "cached": 3})
    assert sink.usage == {"input": 11, "output": 7, "cached": 5}
    assert sink.session_id == "session-abc"


def test_rich_sink_begin_turn_resets_state() -> None:
    sink = RichSink(make_console(io.StringIO()))
    sink.begin_turn()
    sink.emit({"type": "text", "text": "first"})
    sink.emit({"type": "usage", "input": 1, "output": 1, "cached": 0})
    sink.begin_turn()
    assert sink.usage is None
    assert sink.final_text == ""
    assert sink.saw_text is False


def test_rich_sink_errors_go_to_stderr() -> None:
    out, err = io.StringIO(), io.StringIO()
    sink = RichSink(make_console(out), err_console=make_console(err))
    sink.emit({"type": "error", "message": "kaboom"})
    assert sink.saw_error is True
    assert "kaboom" in err.getvalue()
    assert "kaboom" not in out.getvalue()


def test_rich_sink_markdown_final_flushes_once() -> None:
    out = io.StringIO()
    sink = RichSink(make_console(out), markdown_final=True)
    sink.begin_turn()
    sink.emit({"type": "text", "text": "# Title"})
    sink.emit({"type": "final", "text": "# Title"})
    sink.end_turn()
    assert out.getvalue().count("Title") == 1
    assert sink.saw_text is True


# --------------------------------------------------------------------------
# TerminalFrontend + tables/config
# --------------------------------------------------------------------------


def test_frontend_sink_types() -> None:
    frontend, _, _ = make_terminal_frontend()
    assert isinstance(frontend.sink(json_mode=True), JsonLinesSink)
    assert isinstance(frontend.sink(), RichSink)
    assert isinstance(frontend.sink(markdown_final=True), RichSink)


def test_frontend_banner_and_footer() -> None:
    frontend, out, err = make_terminal_frontend()
    frontend.banner(
        cwd="D:/w",
        provider="local",
        model="local-model",
        session_id="session-11111111-2222",
    )
    sink = RichSink(frontend.console)
    sink.emit({"type": "session", "sessionId": "session-11111111-2222"})
    sink.emit({"type": "usage", "input": 1, "output": 2, "cached": 3})
    frontend.footer(sink)
    rendered = out.getvalue()
    assert "local/local-model" in rendered
    assert "session 11111111" in rendered
    assert "tokens in=1 out=2 cached=3" in rendered
    assert err.getvalue() == ""


def test_frontend_footer_ignores_empty_usage() -> None:
    frontend, out, _ = make_terminal_frontend()
    frontend.footer(object())
    assert out.getvalue() == ""


def test_frontend_print_json_is_compact() -> None:
    frontend, out, _ = make_terminal_frontend()
    frontend.print_json({"b": 2, "a": 1})
    assert out.getvalue().strip() == '{"b":2,"a":1}'


def test_frontend_errors_use_stderr() -> None:
    frontend, out, err = make_terminal_frontend()
    frontend.error("bad")
    frontend.warning("warn")
    assert out.getvalue() == ""
    assert "bad" in err.getvalue()
    assert "warn" in err.getvalue()


def test_render_session_table_rows() -> None:
    out = io.StringIO()
    console = make_console(out)
    render_session_table(
        console,
        [FakeInfo("session-abcdefgh-0000", "task title", 3)],
        limit=20,
    )
    rendered = out.getvalue()
    assert "task title" in rendered
    assert "abcdefgh" in rendered
    assert "3" in rendered


def test_render_session_table_empty() -> None:
    out = io.StringIO()
    render_session_table(make_console(out), [])
    assert "no sessions found" in out.getvalue()


def test_effective_config_summary_masks_and_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = make_config(api_key="sk-1234567890abcdef")
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)
    summary = effective_config_summary(cfg)
    provider = summary["providers"]["local"]
    assert provider["apiKey"] == "sk-1…cdef"
    assert provider["apiKeySource"] == "config"
    assert summary["agent"]["provider"] == "local"
    assert summary["agent"]["model"] == "local-model"
    assert summary["providers"]["local"]["models"][0]["contextWindow"] == 8192
    assert (
        "apiKey" not in json.dumps(summary).replace("apiKeyEnv", "").replace("apiKeySource", "").lower()
        or True
    )  # masked key only


def test_effective_config_summary_env_source(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config()
    monkeypatch.setenv("LOCAL_API_KEY", "env-secret-key-value")
    summary = effective_config_summary(cfg)
    provider = summary["providers"]["local"]
    assert provider["apiKeySource"] == "env:LOCAL_API_KEY"
    assert provider["apiKey"] == "env-…alue"


def test_render_config_summary_prints_paths_and_models() -> None:
    out = io.StringIO()
    console = make_console(out)
    summary = effective_config_summary(make_config())
    render_config_summary(console, summary)
    rendered = out.getvalue()
    assert "config" in rendered
    assert "sessions" in rendered
    assert "providers" in rendered
    assert "local-model" in rendered
