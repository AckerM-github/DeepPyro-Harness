"""Tests for the ask_user_question tool, schema and fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext
from deeppyro.tools.builtin.ask_user import AskUserQuestionTool
from deeppyro.user_questions import Answer, AskRequest, UserQuestionsService, parse_questions

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
SESSION_DIR = FIXTURES / "sessions"
REQUEST_HEADER = FIXTURES / "records" / "by-type" / "request_header.jsonl"


def make_context(
    base: Path, services: dict[str, Any] | None = None, call_id: str = "call_ask"
) -> ToolContext:
    return ToolContext(
        cwd=base,
        session_id="session-tool",
        turn=0,
        step=0,
        call_id=call_id,
        cancel=asyncio.Event(),
        log=lambda kind, data: None,
        services={} if services is None else services,
    )


def load_ask_fixtures() -> list[dict[str, Any]]:
    """Join each real ask_user_question call with its recorded result."""
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    if not SESSION_DIR.is_dir():
        return []
    for path in sorted(SESSION_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = record.get("type")
            data = record.get("data") or {}
            if kind == "tool/call" and data.get("name") == "ask_user_question":
                calls[data["callId"]] = data
            elif kind == "tool/result":
                message = data.get("message") or {}
                call_id = message.get("toolCallId")
                if call_id in calls:
                    text = "".join(part.get("text", "") for part in (message.get("content") or []))
                    results[call_id] = {"text": text, "isError": bool(message.get("isError"))}
    joined: list[dict[str, Any]] = []
    for call_id, call in calls.items():
        arguments = json.loads(call["arguments"])
        result = results.get(call_id)
        joined.append(
            {
                "call_id": call_id,
                "arguments": arguments,
                "result": result,
            }
        )
    return joined


FIXTURE_CALLS = load_ask_fixtures()


def test_schema_matches_real_dsh_tool_schema() -> None:
    tool = AskUserQuestionTool()
    line = next(line for line in REQUEST_HEADER.read_text(encoding="utf-8").splitlines() if line.strip())
    tools = json.loads(line)["data"]["header"]["tools"]
    expected = next(item for item in tools if item["name"] == "ask_user_question")
    assert tool.name == "ask_user_question"
    assert tool.description == expected["description"]
    assert tool.parameters == expected["parameters"]
    assert tool.parallel_safe is False


async def test_missing_service_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ToolError) as excinfo:
        await AskUserQuestionTool().run(
            {"questions": [{"id": "q", "question": "Q?"}]}, make_context(tmp_path, {})
        )
    assert str(excinfo.value) == "ask_user_question is not available in this session"


async def test_invalid_arguments_are_rejected(tmp_path: Path) -> None:
    service = UserQuestionsService()
    ctx = make_context(tmp_path, {"user_questions": service})
    with pytest.raises(ToolError) as excinfo:
        await AskUserQuestionTool().run({"questions": []}, ctx)
    assert str(excinfo.value) == "questions must be a non-empty list"
    with pytest.raises(ToolError) as excinfo:
        await AskUserQuestionTool().run({}, ctx)
    assert str(excinfo.value) == "questions must be a non-empty list"


async def test_successful_call_returns_compact_json_and_meta(tmp_path: Path) -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        return [
            Answer(id="proceed", selected=("Yes",)),
            Answer(id="note", selected=(), custom="later"),
        ]

    service.configure(handler)
    tool = AskUserQuestionTool()
    result = await tool.run(
        {
            "questions": [
                {"id": "proceed", "question": "Proceed?", "options": [{"label": "Yes"}]},
                {"id": "note", "question": "Anything else?", "multi_select": True},
            ]
        },
        make_context(tmp_path, {"user_questions": service}, call_id="call_9"),
    )
    expected_answers = [
        {"id": "proceed", "selected": ["Yes"]},
        {"id": "note", "selected": [], "custom": "later"},
    ]
    assert result.is_error is False
    assert result.text == json.dumps({"answers": expected_answers}, ensure_ascii=False, separators=(",", ":"))
    assert result.meta == {"callId": "call_9", "answers": expected_answers}


def test_every_fixture_call_parses() -> None:
    if not FIXTURE_CALLS:
        pytest.skip("ask_user_question fixture calls are unavailable")
    assert len(FIXTURE_CALLS) == 8
    for fixture in FIXTURE_CALLS:
        questions = parse_questions(fixture["arguments"].get("questions"))
        assert questions
        assert all(question.id.strip() for question in questions)


@pytest.mark.parametrize(
    "fixture",
    [item for item in FIXTURE_CALLS if item["result"] and not item["result"]["isError"]],
)
async def test_successful_fixture_replays_byte_for_byte(fixture: dict[str, Any], tmp_path: Path) -> None:
    expected_payload = json.loads(fixture["result"]["text"])
    expected_answers = expected_payload["answers"]
    answers = [
        Answer(
            id=item["id"],
            selected=tuple(item.get("selected", [])),
            custom=item.get("custom"),
        )
        for item in expected_answers
    ]
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        return answers

    service.configure(handler)
    tool = AskUserQuestionTool()
    result = await tool.run(
        fixture["arguments"],
        make_context(tmp_path, {"user_questions": service}, call_id=fixture["call_id"]),
    )
    assert result.is_error is False
    assert result.text == fixture["result"]["text"]
    assert result.meta == {"callId": fixture["call_id"], "answers": expected_answers}


async def test_error_fixture_surfaces_the_handler_message(tmp_path: Path) -> None:
    failed = [item for item in FIXTURE_CALLS if item["result"]["isError"]]
    if not failed:
        pytest.skip("ask_user_question error fixture is unavailable")
    fixture = failed[0]
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        raise RuntimeError("dsh-im interaction adapter was disposed")

    service.configure(handler)
    with pytest.raises(ToolError) as excinfo:
        await AskUserQuestionTool().run(
            fixture["arguments"],
            make_context(tmp_path, {"user_questions": service}, call_id=fixture["call_id"]),
        )
    assert str(excinfo.value) == "failed to ask the user: dsh-im interaction adapter was disposed"


def test_plugin_provides_service_and_registers_tool(tmp_path: Path) -> None:
    from deeppyro.builtin.user_questions import UserQuestionsPlugin

    class FakeCtx:
        def __init__(self) -> None:
            self.provided: dict[str, Any] = {}
            self.factories: list[Any] = []

        def provide(self, name: str, value: Any) -> None:
            self.provided[name] = value

        def register_tool(self, factory: Any) -> None:
            self.factories.append(factory)

    ctx = FakeCtx()
    plugin = UserQuestionsPlugin(ctx, SimpleNamespace())
    assert plugin.provide == "user_questions"
    plugin.setup(ctx, plugin.config)
    assert ctx.provided["user_questions"] is plugin.service
    assert isinstance(ctx.provided["user_questions"], UserQuestionsService)
    assert len(ctx.factories) == 1
    tools = ctx.factories[0](None, tmp_path)
    assert [tool.name for tool in tools] == ["ask_user_question"]
    assert isinstance(tools[0], AskUserQuestionTool)
