"""Blocking user-question service shared by tools and the TUI."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, ClassVar

from deeppyro.core.errors import ToolError
from deeppyro.user_questions.types import (
    Answer,
    AskHandler,
    AskRequest,
    Question,
    validate_questions,
)


class UserQuestionsService:
    """Ask the host user a question and normalise the answer payload.

    The service itself never touches the terminal.  The TUI (or headless host)
    installs an AskHandler with configure(); without one the tool reports that
    asking is unavailable.  Requests are serialised so only one modal question
    is active at a time.
    """

    provide: ClassVar[str] = "user_questions"
    inject: ClassVar[list[str]] = ["tools"]

    def __init__(self) -> None:
        self._handler: AskHandler | None = None
        self._lock = asyncio.Lock()

    @property
    def handler(self) -> AskHandler | None:
        """The currently installed answerer, if any."""
        return self._handler

    def configure(self, handler: AskHandler | None) -> None:
        """Install (or clear) the host answerer."""
        if handler is not None and not callable(handler):
            raise ToolError("ask_user_question handler must be callable")
        self._handler = handler

    async def ask(
        self,
        questions: Sequence[Question],
        *,
        call_id: str,
        cancel: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Ask all questions in one blocking request and return the payload.

        Returns {"answers": [...]} in question order.  A question without an
        answer is reported as skipped (selected empty, no custom).
        """
        parsed = validate_questions(questions)
        handler = self._handler
        if handler is None:
            raise ToolError("ask_user_question is not available in this session")
        async with self._lock:
            if cancel is not None and cancel.is_set():
                raise ToolError("question was cancelled")
            raw = await self._invoke(handler, AskRequest(questions=parsed, call_id=call_id), cancel)
        return {"answers": self._normalise_answers(raw, parsed)}

    async def _invoke(
        self,
        handler: AskHandler,
        request: AskRequest,
        cancel: asyncio.Event | None,
    ) -> list[Answer]:
        task = asyncio.ensure_future(handler(request))
        if cancel is None:
            try:
                return await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise ToolError(f"failed to ask the user: {exc}") from exc
        waiter = asyncio.ensure_future(cancel.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                try:
                    return await task
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise ToolError(f"failed to ask the user: {exc}") from exc
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise ToolError("question was cancelled")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    @staticmethod
    def _normalise_answers(raw: Any, questions: tuple[Question, ...]) -> list[dict[str, Any]]:
        if not isinstance(raw, (list, tuple)):
            raise ToolError("the answerer returned an invalid answer list")
        known = {question.id for question in questions}
        answers: dict[str, dict[str, Any]] = {}
        for index, answer in enumerate(raw):
            if not isinstance(answer, Answer):
                raise ToolError(f"answer at index {index} must be an Answer")
            if answer.id not in known:
                raise ToolError(f'answer id "{answer.id}" does not match any question')
            if answer.id in answers:
                raise ToolError(f'duplicate answer for question "{answer.id}"')
            selected = answer.selected
            if not isinstance(selected, (list, tuple)) or not all(isinstance(item, str) for item in selected):
                raise ToolError(f'answer for question "{answer.id}" selected must be a list of strings')
            if answer.custom is not None and not isinstance(answer.custom, str):
                raise ToolError(f'answer for question "{answer.id}" custom must be a string')
            payload: dict[str, Any] = {"id": answer.id, "selected": list(selected)}
            if answer.custom is not None:
                payload["custom"] = answer.custom
            answers[answer.id] = payload
        return [answers.get(question.id) or {"id": question.id, "selected": []} for question in questions]


__all__ = ["UserQuestionsService"]
