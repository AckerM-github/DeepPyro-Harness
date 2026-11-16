"""Layer 3 of the DeepPyro TUI app: Approval and user-question interaction layer."""

from __future__ import annotations

import asyncio
from typing import Any

from deeppyro.tui.app_events import EventLayer
from deeppyro.tui.screens.approval import ALLOW_ONCE, ApprovalScreen
from deeppyro.tui.screens.question import QuestionScreen
from deeppyro.user_questions.types import Answer, AskRequest


class ApprovalLayer(EventLayer):
    """Approval and user-question interaction layer."""

    def _configure_user_questions(self) -> None:
        """Route ask_user_question requests to the TUI modal handler."""
        configure = getattr(self.runtime, "configure_user_questions", None)
        if callable(configure):
            configure(self.ask_question)

    # -- approval --------------------------------------------------------
    async def ask_approval(self, request: Any) -> bool:
        """Serve one permission request with the approval modal."""
        try:
            decision = await self.push_screen_wait(ApprovalScreen(request))
        except Exception:  # noqa: BLE001 - outside a worker there is no modal
            return False
        return decision in (ALLOW_ONCE, "once")

    # -- questions -------------------------------------------------------
    async def ask_question(self, request: AskRequest) -> list[Answer]:
        """Serve one ask_user_question request with a modal per question.

        Escape/Cancel in a modal stops the request: answers already collected
        are returned and the remaining questions are not shown, which the
        user_questions service normalises into skipped answers. A failing or
        closed modal never raises into the agent turn: the answers collected
        so far (possibly none) are returned instead.
        """
        questions = tuple(getattr(request, "questions", ()) or ())
        answers: list[Answer] = []
        if not questions:
            return answers
        total = len(questions)
        for index, question in enumerate(questions):
            try:
                answer = await self.push_screen_wait(QuestionScreen(question, index=index, total=total))
            except asyncio.CancelledError:
                # An interrupted turn cancels the wait: drop the orphaned modal
                # so the next turn does not start behind a stale screen.
                self._dismiss_question_screen()
                return answers
            except Exception:  # noqa: BLE001 - UI failure must not hang the agent
                return answers
            if answer is None:
                break
            answers.append(answer)
        return answers

    def _dismiss_question_screen(self) -> None:
        """Pop the question modal if a cancelled wait left it on top."""
        try:
            screen = self.screen
        except Exception:  # noqa: BLE001 - the screen stack may already be tearing down
            return
        if not isinstance(screen, QuestionScreen):
            return
        try:
            self.pop_screen()
        except Exception:  # noqa: BLE001 - a modal already gone must not raise
            return
