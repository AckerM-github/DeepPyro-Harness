"""Types and argument parsing for the ask_user_question tool."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from deeppyro.core.errors import ToolError


@dataclass(frozen=True, slots=True)
class QuestionOption:
    """One selectable option shown to the user."""

    label: str
    description: str | None = None


@dataclass(frozen=True, slots=True)
class Question:
    """One question in a single ask_user_question call."""

    id: str
    question: str
    header: str | None = None
    options: tuple[QuestionOption, ...] = ()
    multi_select: bool = False


@dataclass(frozen=True, slots=True)
class Answer:
    """The user's answer to one question.

    An empty selected tuple with no custom text means the user skipped the
    question.  A skip is not permission and the model is told not to treat it
    as one.
    """

    id: str
    selected: tuple[str, ...] = ()
    custom: str | None = None


@dataclass(frozen=True, slots=True)
class AskRequest:
    """One blocking request handed to the host answerer."""

    questions: tuple[Question, ...]
    call_id: str


#: Host answerer installed by the TUI; returns one Answer per question.
AskHandler = Callable[[AskRequest], Awaitable[list[Answer]]]


def _non_empty(value: Any, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(message)
    return value


def parse_questions(value: Any) -> tuple[Question, ...]:
    """Parse and validate the raw model-facing questions array.

    Unknown fields are ignored: the DSH schema sets
    additionalProperties: true on question and option objects.
    """
    if not isinstance(value, list) or not value:
        raise ToolError("questions must be a non-empty list")
    questions: list[Question] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ToolError(f"question at index {index} must be an object")
        question_id = _non_empty(item.get("id"), f"question at index {index} must have a non-empty id")
        if question_id in seen:
            raise ToolError(f'duplicate question id "{question_id}"')
        seen.add(question_id)
        text = _non_empty(item.get("question"), f"question at index {index} must have a non-empty question")
        header = item.get("header")
        if header is not None and not isinstance(header, str):
            raise ToolError(f"question at index {index}: header must be a string")
        options: tuple[QuestionOption, ...] = ()
        raw_options = item.get("options")
        if raw_options is not None:
            if not isinstance(raw_options, list):
                raise ToolError(f"question at index {index}: options must be a list")
            parsed_options: list[QuestionOption] = []
            for option_index, raw_option in enumerate(raw_options):
                if not isinstance(raw_option, dict):
                    raise ToolError(
                        f"question at index {index}: option at index {option_index} must be an object"
                    )
                label = _non_empty(
                    raw_option.get("label"),
                    f"question at index {index}: option at index {option_index} must have a non-empty label",
                )
                description = raw_option.get("description")
                if description is not None and not isinstance(description, str):
                    raise ToolError(f"question at index {index}: option description must be a string")
                parsed_options.append(QuestionOption(label=label, description=description))
            options = tuple(parsed_options)
        multi_select = False
        if "multi_select" in item:
            multi_select = item.get("multi_select")
            if not isinstance(multi_select, bool):
                raise ToolError(f"question at index {index}: multi_select must be a boolean")
        questions.append(
            Question(
                id=question_id,
                question=text,
                header=header,
                options=options,
                multi_select=multi_select,
            )
        )
    return tuple(questions)


def validate_questions(questions: Any) -> tuple[Question, ...]:
    """Validate already-constructed Question objects accepted by the service."""
    if not isinstance(questions, (list, tuple)) or not questions:
        raise ToolError("questions must contain at least one question")
    validated: list[Question] = []
    seen: set[str] = set()
    for index, question in enumerate(questions):
        if not isinstance(question, Question):
            raise ToolError(f"question at index {index} must be a Question")
        if not isinstance(question.id, str) or not question.id.strip():
            raise ToolError(f"question at index {index} must have a non-empty id")
        if question.id in seen:
            raise ToolError(f'duplicate question id "{question.id}"')
        seen.add(question.id)
        if not isinstance(question.question, str) or not question.question.strip():
            raise ToolError(f"question at index {index} must have a non-empty question")
        if question.header is not None and not isinstance(question.header, str):
            raise ToolError(f"question at index {index}: header must be a string")
        if not isinstance(question.options, (list, tuple)):
            raise ToolError(f"question at index {index}: options must be a list")
        for option in question.options:
            if not isinstance(option, QuestionOption):
                raise ToolError(f"question at index {index}: options must be QuestionOption values")
            if not isinstance(option.label, str) or not option.label.strip():
                raise ToolError(f"question at index {index}: option label must not be empty")
            if option.description is not None and not isinstance(option.description, str):
                raise ToolError(f"question at index {index}: option description must be a string")
        if not isinstance(question.multi_select, bool):
            raise ToolError(f"question at index {index}: multi_select must be a boolean")
        validated.append(
            Question(
                id=question.id,
                question=question.question,
                header=question.header,
                options=tuple(question.options),
                multi_select=question.multi_select,
            )
        )
    return tuple(validated)


__all__ = [
    "Answer",
    "AskHandler",
    "AskRequest",
    "Question",
    "QuestionOption",
    "parse_questions",
    "validate_questions",
]
