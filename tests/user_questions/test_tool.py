"""Tests for the ask_user_question tool, schema and plugin."""

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

SCHEMA_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "native" / "tool_schemas.json"


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


def test_schema_matches_native_tool_schema() -> None:
    tool = AskUserQuestionTool()
    tools = json.loads(SCHEMA_FIXTURE.read_text(encoding="utf-8"))["tools"]
    expected = tools["ask_user_question"]
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


async def test_questions_are_parsed_from_model_arguments(tmp_path: Path) -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        return [Answer(id=question.id, selected=()) for question in request.questions]

    service.configure(handler)
    result = await AskUserQuestionTool().run(
        {
            "questions": [
                {
                    "id": "proceed",
                    "header": "Confirm",
                    "question": "Proceed with the cleanup?",
                    "options": [{"label": "Yes", "description": "Delete the stale files."}],
                    "multi_select": True,
                }
            ]
        },
        make_context(tmp_path, {"user_questions": service}),
    )
    assert result.is_error is False
    assert json.loads(result.text) == {"answers": [{"id": "proceed", "selected": []}]}
    assert [question.id for question in parse_questions([{"id": "a", "question": "A?"}])] == ["a"]


async def test_handler_failure_surfaces_the_handler_message(tmp_path: Path) -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        raise RuntimeError("dsh-im interaction adapter was disposed")

    service.configure(handler)
    with pytest.raises(ToolError) as excinfo:
        await AskUserQuestionTool().run(
            {"questions": [{"id": "proceed", "question": "Proceed?"}]},
            make_context(tmp_path, {"user_questions": service}),
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
    plugin.setup()
    assert ctx.provided["user_questions"] is plugin.service
    assert isinstance(ctx.provided["user_questions"], UserQuestionsService)
    assert len(ctx.factories) == 1
    tools = ctx.factories[0](None, tmp_path)
    assert [tool.name for tool in tools] == ["ask_user_question"]
    assert isinstance(tools[0], AskUserQuestionTool)
