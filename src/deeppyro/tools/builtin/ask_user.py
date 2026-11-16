"""ask_user_question: block the turn on a question for the user."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ConfigDict, Field

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool
from deeppyro.tools.params import ToolArgs
from deeppyro.user_questions.types import parse_questions


class AskOption(ToolArgs):
    """One selectable answer (extra keys are tolerated on the wire)."""

    model_config = ConfigDict(extra="allow")

    label: str = Field(description="Short user-facing option label.")
    description: str | None = Field(
        default=None,
        description="One sentence explaining the tradeoff or impact.",
    )


class AskQuestion(ToolArgs):
    """One question for the user (extra keys are tolerated on the wire)."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(description="Stable id for this question; echoed in the answer.")
    question: str = Field(description="The specific question to ask the user.")
    header: str | None = Field(
        default=None,
        description='Optional short heading for the question, such as "Confirm" or "Choose Mode".',
    )
    options: list[AskOption] | None = Field(
        default=None,
        description=(
            "Optional choices to show the user. If you recommend one, put it "
            'first and append "(Recommended)" to that label.'
        ),
    )
    multi_select: bool | None = Field(
        default=None,
        description="Whether the user may select more than one option. Defaults to false.",
    )


class AskUserQuestionTool(BaseTool):
    """Ask the user one or more questions and return their answers."""

    name = "ask_user_question"
    description = (
        "Ask the user a concise question when you need confirmation, a choice, or missing "
        "information before proceeding. An answer item with empty selected and no custom means the "
        "user skipped that question: continue without it, do not ask it again, and do not treat the "
        "skip as permission."
    )
    parallel_safe = False

    class Args(ToolArgs):
        """Arguments for ask_user_question.

        The historical schema has no top-level additionalProperties and lets
        questions/options carry extra keys, so both deviate from the default.
        """

        SCHEMA_TOP_LEVEL_ADDITIONAL_PROPERTIES = None

        questions: list[AskQuestion] = Field(description="Questions to ask the user before continuing.")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        service = (ctx.services or {}).get("user_questions")
        if service is None:
            raise ToolError("ask_user_question is not available in this session")
        questions = parse_questions(args.get("questions"))
        payload = await service.ask(questions, call_id=ctx.call_id, cancel=ctx.cancel)
        answers = payload.get("answers", [])
        return ToolResult(
            text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            meta={"callId": ctx.call_id, "answers": answers},
        )


__all__ = ["AskUserQuestionTool"]
