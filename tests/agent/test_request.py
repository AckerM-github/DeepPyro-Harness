"""Tests for request/header bookkeeping."""

from __future__ import annotations

from types import SimpleNamespace

from deeppyro.agent.request import (
    build_header,
    context_data,
    latest_context,
    latest_header,
    latest_header_seq,
    plan_header,
    tool_change_data,
    tool_changes,
)

TOOLS = [
    {"name": "read", "description": "read a file", "parameters": {"type": "object"}},
    {"name": "pwd", "description": "working directory", "parameters": {"type": "object"}},
]


def record(kind: str, seq: int, data: dict):
    return SimpleNamespace(type=kind, seq=seq, data=data)


def header_record(seq: int = 3, *, tools=TOOLS, header: dict | None = None):
    value = (
        header
        if header is not None
        else build_header(provider="deepseek", model="deepseek-v4-pro", max_tokens=8192, tools=tools)
    )
    return record("request/header", seq, {"turn": 1, "step": 1, "header": value, "reason": "initial"})


def test_build_header_shape_and_tool_order():
    header = build_header(provider="deepseek", model="deepseek-v4-pro", max_tokens=8192, tools=TOOLS)
    assert header["config"] == {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "maxTokens": 8192,
        "reasoningEffort": None,
    }
    assert header["adapterDefaults"] == {"reasoningEffort": True, "maxTokens": True}
    assert [tool["name"] for tool in header["tools"]] == ["read", "pwd"]


def test_plan_header_initial_for_empty_session():
    plan = plan_header(
        [],
        provider="deepseek",
        model="deepseek-v4-pro",
        max_tokens=8192,
        tools=TOOLS,
        starts_series=True,
        logged=False,
    )
    assert plan.write is True
    assert plan.reason == "initial"


def test_plan_header_resume_when_session_already_has_header():
    plan = plan_header(
        [header_record()],
        provider="deepseek",
        model="deepseek-v4-pro",
        max_tokens=8192,
        tools=TOOLS,
        starts_series=True,
        logged=False,
    )
    assert plan.write is True
    assert plan.reason == "resume"


def test_plan_header_series_when_unchanged_and_new_series():
    plan = plan_header(
        [header_record()],
        provider="deepseek",
        model="deepseek-v4-pro",
        max_tokens=8192,
        tools=TOOLS,
        starts_series=True,
        logged=True,
    )
    assert plan.write is True
    assert plan.reason == "series"


def test_plan_header_no_write_when_unchanged_and_not_new_series():
    plan = plan_header(
        [header_record()],
        provider="deepseek",
        model="deepseek-v4-pro",
        max_tokens=8192,
        tools=TOOLS,
        starts_series=False,
        logged=True,
    )
    assert plan.write is False
    assert plan.reason is None


def test_plan_header_change_when_config_changes():
    plan = plan_header(
        [header_record()],
        provider="deepseek",
        model="deepseek-chat",
        max_tokens=8192,
        tools=TOOLS,
        starts_series=False,
        logged=True,
    )
    assert plan.write is True
    assert plan.reason == "change"


def test_plan_header_change_when_tools_change():
    plan = plan_header(
        [header_record()],
        provider="deepseek",
        model="deepseek-v4-pro",
        max_tokens=8192,
        tools=[TOOLS[0]],
        starts_series=False,
        logged=True,
    )
    assert plan.write is True
    assert plan.reason == "change"


def test_tool_changes_reports_additions_and_removals():
    old = build_header(provider="p", model="m", max_tokens=1, tools=[TOOLS[0]])
    change = tool_changes(old, [TOOLS[1], TOOLS[0], {"name": "new"}])
    assert [tool["name"] for tool in change.additions] == ["pwd", "new"]
    assert [tool["name"] for tool in change.removals] == []
    assert change.changed is True

    removed = tool_changes(old, [])
    assert removed.additions == ()
    assert [tool["name"] for tool in removed.removals] == ["read"]


def test_tool_change_data_message_shape_and_header_seq():
    change = tool_changes(
        build_header(provider="p", model="m", max_tokens=1, tools=[]),
        [TOOLS[0], TOOLS[1]],
    )
    data = tool_change_data(2, 1, change, header_seq=7)
    assert data is not None
    assert data["turn"] == 2
    assert data["step"] == 1
    assert data["headerSeq"] == 7
    message = data["message"]
    assert message["role"] == "developer"
    assert message["source"] == {"kind": "tool-registry"}
    assert message["content"] == [
        {"type": "tool-addition", "toolName": "read"},
        {"type": "tool-addition", "toolName": "pwd"},
    ]
    assert message["id"]


def test_tool_change_data_removals_have_no_header_seq():
    change = tool_changes(
        build_header(provider="p", model="m", max_tokens=1, tools=TOOLS),
        [],
    )
    data = tool_change_data(1, 2, change, header_seq=9)
    assert data is not None
    assert "headerSeq" not in data
    assert data["message"]["content"] == [
        {"type": "tool-removal", "toolName": "read"},
        {"type": "tool-removal", "toolName": "pwd"},
    ]


def test_tool_change_data_none_when_unchanged():
    change = tool_changes(
        build_header(provider="p", model="m", max_tokens=1, tools=TOOLS),
        TOOLS,
    )
    assert tool_change_data(1, 1, change) is None


def test_context_data_only_when_changed():
    records = []
    first = context_data(
        records,
        provider="deepseek",
        model="deepseek-v4-pro",
        context_window=32768,
        turn=1,
        step=1,
    )
    assert first == {
        "turn": 1,
        "step": 1,
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "contextWindow": 32768,
        "systemPromptUpdate": "in-history",
    }
    records.append(record("request/context", 4, first))
    assert (
        context_data(
            records,
            provider="deepseek",
            model="deepseek-v4-pro",
            context_window=32768,
            turn=2,
            step=1,
        )
        is None
    )
    changed = context_data(
        records,
        provider="deepseek",
        model="deepseek-chat",
        context_window=32768,
        turn=2,
        step=1,
    )
    assert changed is not None
    assert changed["model"] == "deepseek-chat"


def test_latest_helpers_ignore_other_records():
    records = [
        record("user/message", 1, {}),
        header_record(2),
        record("request/context", 3, {"provider": "p"}),
        record("assistant/message", 4, {}),
    ]
    assert latest_header(records) is not None
    assert latest_header_seq(records) == 2
    assert latest_context(records) == {"provider": "p"}
