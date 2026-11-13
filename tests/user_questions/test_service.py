"""Tests for the blocking user-questions service."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from deeppyro.core.errors import ToolError
from deeppyro.user_questions import (
    Answer,
    AskRequest,
    Question,
    QuestionOption,
    UserQuestionsService,
    parse_questions,
    validate_questions,
)


def make_questions() -> tuple[Question, ...]:
    return (
        Question(
            id="proceed",
            question="Proceed?",
            header="Confirm",
            options=(QuestionOption("Yes", "Go ahead"), QuestionOption("No")),
        ),
        Question(id="note", question="Anything else?", multi_select=True),
    )


async def test_parse_questions_builds_full_objects() -> None:
    parsed = parse_questions(
        [
            {
                "id": "proceed",
                "question": "Proceed?",
                "header": "Confirm",
                "options": [
                    {"label": "Yes", "description": "Go ahead"},
                    {"label": "No"},
                ],
                "multi_select": True,
                "unknown": "ignored",
            }
        ]
    )
    assert parsed == (
        Question(
            id="proceed",
            question="Proceed?",
            header="Confirm",
            options=(QuestionOption("Yes", "Go ahead"), QuestionOption("No")),
            multi_select=True,
        ),
    )


async def test_parse_questions_defaults() -> None:
    parsed = parse_questions([{"id": "q", "question": "Q?"}])
    assert parsed[0].header is None
    assert parsed[0].options == ()
    assert parsed[0].multi_select is False


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "questions must be a non-empty list"),
        ("nope", "questions must be a non-empty list"),
        ([], "questions must be a non-empty list"),
        ([1], "question at index 0 must be an object"),
        ([{"question": "Q?"}], "question at index 0 must have a non-empty id"),
        ([{"id": " ", "question": "Q?"}], "question at index 0 must have a non-empty id"),
        ([{"id": "q", "question": " "}], "question at index 0 must have a non-empty question"),
        (
            [{"id": "q", "question": "Q?"}, {"id": "q", "question": "Again?"}],
            'duplicate question id "q"',
        ),
        ([{"id": "q", "question": "Q?", "header": 1}], "question at index 0: header must be a string"),
        (
            [{"id": "q", "question": "Q?", "options": "yes"}],
            "question at index 0: options must be a list",
        ),
        (
            [{"id": "q", "question": "Q?", "options": [1]}],
            "question at index 0: option at index 0 must be an object",
        ),
        (
            [{"id": "q", "question": "Q?", "options": [{}]}],
            "question at index 0: option at index 0 must have a non-empty label",
        ),
        (
            [{"id": "q", "question": "Q?", "options": [{"label": "L", "description": 2}]}],
            "question at index 0: option description must be a string",
        ),
        (
            [{"id": "q", "question": "Q?", "multi_select": "yes"}],
            "question at index 0: multi_select must be a boolean",
        ),
    ],
)
def test_parse_questions_rejects_invalid_input(value: Any, message: str) -> None:
    with pytest.raises(ToolError) as excinfo:
        parse_questions(value)
    assert str(excinfo.value) == message


def test_validate_questions_rejects_invalid_objects() -> None:
    with pytest.raises(ToolError) as excinfo:
        validate_questions([])
    assert str(excinfo.value) == "questions must contain at least one question"
    with pytest.raises(ToolError) as excinfo:
        validate_questions([{"id": "q", "question": "Q?"}])
    assert str(excinfo.value) == "question at index 0 must be a Question"
    with pytest.raises(ToolError) as excinfo:
        validate_questions([Question(id="q", question="Q?"), Question(id="q", question="R?")])
    assert str(excinfo.value) == 'duplicate question id "q"'
    with pytest.raises(ToolError) as excinfo:
        validate_questions([Question(id="q", question=" ")])
    assert str(excinfo.value) == "question at index 0 must have a non-empty question"
    with pytest.raises(ToolError) as excinfo:
        validate_questions([Question(id="q", question="Q?", header=3)])  # type: ignore[arg-type]
    assert str(excinfo.value) == "question at index 0: header must be a string"
    with pytest.raises(ToolError) as excinfo:
        validate_questions([Question(id="q", question="Q?", options=("bad",))])  # type: ignore[arg-type]
    assert str(excinfo.value) == "question at index 0: options must be QuestionOption values"
    with pytest.raises(ToolError) as excinfo:
        validate_questions([Question(id="q", question="Q?", options=(QuestionOption(" "),))])
    assert str(excinfo.value) == "question at index 0: option label must not be empty"


async def test_no_handler_is_explicitly_unavailable() -> None:
    service = UserQuestionsService()
    with pytest.raises(ToolError) as excinfo:
        await service.ask(make_questions(), call_id="call_1")
    assert str(excinfo.value) == "ask_user_question is not available in this session"


def test_configure_rejects_non_callable() -> None:
    service = UserQuestionsService()
    with pytest.raises(ToolError) as excinfo:
        service.configure(object())  # type: ignore[arg-type]
    assert str(excinfo.value) == "ask_user_question handler must be callable"


async def test_ask_returns_answers_in_question_order() -> None:
    service = UserQuestionsService()
    seen: list[AskRequest] = []

    async def handler(request: AskRequest) -> list[Answer]:
        seen.append(request)
        return [
            Answer(id="note", selected=("second",)),  # out of order on purpose
            Answer(id="proceed", selected=("Yes",)),
        ]

    service.configure(handler)
    payload = await service.ask(make_questions(), call_id="call_abc")

    assert payload == {
        "answers": [
            {"id": "proceed", "selected": ["Yes"]},
            {"id": "note", "selected": ["second"]},
        ]
    }
    assert len(seen) == 1
    assert seen[0].call_id == "call_abc"
    assert [question.id for question in seen[0].questions] == ["proceed", "note"]


async def test_custom_answer_is_kept_and_none_is_omitted() -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        return [Answer(id="proceed", selected=(), custom="do it later"), Answer(id="note")]

    service.configure(handler)
    payload = await service.ask(make_questions(), call_id="call_1")
    assert payload["answers"][0] == {
        "id": "proceed",
        "selected": [],
        "custom": "do it later",
    }
    assert payload["answers"][1] == {"id": "note", "selected": []}
    assert "custom" not in payload["answers"][1]


async def test_missing_answers_are_reported_as_skipped() -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        return []

    service.configure(handler)
    payload = await service.ask(make_questions(), call_id="call_1")
    assert payload == {
        "answers": [
            {"id": "proceed", "selected": []},
            {"id": "note", "selected": []},
        ]
    }


async def test_handler_errors_are_wrapped() -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        raise RuntimeError("boom")

    service.configure(handler)
    with pytest.raises(ToolError) as excinfo:
        await service.ask(make_questions(), call_id="call_1")
    assert str(excinfo.value) == "failed to ask the user: boom"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"answers": []}, "the answerer returned an invalid answer list"),
        ([{"id": "proceed"}], "answer at index 0 must be an Answer"),
        ([Answer(id="unknown")], 'answer id "unknown" does not match any question'),
        (
            [Answer(id="proceed"), Answer(id="proceed")],
            'duplicate answer for question "proceed"',
        ),
        (
            [Answer(id="proceed", selected="Yes")],  # type: ignore[arg-type]
            'answer for question "proceed" selected must be a list of strings',
        ),
        (
            [Answer(id="proceed", selected=(1,))],  # type: ignore[arg-type]
            'answer for question "proceed" selected must be a list of strings',
        ),
        (
            [Answer(id="proceed", custom=1)],  # type: ignore[arg-type]
            'answer for question "proceed" custom must be a string',
        ),
    ],
)
async def test_invalid_answers_are_rejected(raw: Any, message: str) -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> Any:
        return raw

    service.configure(handler)
    with pytest.raises(ToolError) as excinfo:
        await service.ask(make_questions(), call_id="call_1")
    assert str(excinfo.value) == message


async def test_already_cancelled_request_is_rejected() -> None:
    service = UserQuestionsService()
    called = False

    async def handler(request: AskRequest) -> list[Answer]:
        nonlocal called
        called = True
        return []

    service.configure(handler)
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(ToolError) as excinfo:
        await service.ask(make_questions(), call_id="call_1", cancel=cancel)
    assert str(excinfo.value) == "question was cancelled"
    assert called is False


async def test_in_flight_cancellation_cancels_the_handler() -> None:
    service = UserQuestionsService()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def handler(request: AskRequest) -> list[Answer]:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    service.configure(handler)
    cancel = asyncio.Event()
    task = asyncio.create_task(service.ask(make_questions(), call_id="call_1", cancel=cancel))
    await started.wait()
    cancel.set()
    with pytest.raises(ToolError) as excinfo:
        await task
    assert str(excinfo.value) == "question was cancelled"
    assert finished.is_set() is True


async def test_concurrent_requests_are_serialised() -> None:
    service = UserQuestionsService()
    release = asyncio.Event()
    started = asyncio.Event()
    active = 0
    max_active = 0

    async def handler(request: AskRequest) -> list[Answer]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        started.set()
        await release.wait()
        active -= 1
        return []

    service.configure(handler)
    first = asyncio.create_task(service.ask([Question(id="a", question="A?")], call_id="call_1"))
    await started.wait()
    second = asyncio.create_task(service.ask([Question(id="b", question="B?")], call_id="call_2"))
    for _ in range(10):
        await asyncio.sleep(0)
    assert max_active == 1
    assert second.done() is False
    release.set()
    await asyncio.gather(first, second)
    assert max_active == 1


async def test_configure_none_clears_the_handler() -> None:
    service = UserQuestionsService()

    async def handler(request: AskRequest) -> list[Answer]:
        return []

    service.configure(handler)
    assert service.handler is handler
    service.configure(None)
    assert service.handler is None
    with pytest.raises(ToolError) as excinfo:
        await service.ask(make_questions(), call_id="call_1")
    assert str(excinfo.value) == "ask_user_question is not available in this session"
