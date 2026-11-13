"""ask_user_question: block the turn on a question for the user."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from deeppyro.core.errors import ToolError
from deeppyro.core.protocols import ToolContext, ToolResult
from deeppyro.tools.base import BaseTool
from deeppyro.user_questions.types import parse_questions


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
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to ask the user before continuing.",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Stable id for this question; echoed in the answer.",
                        },
                        "question": {
                            "type": "string",
                            "description": "The specific question to ask the user.",
                        },
                        "header": {
                            "type": "string",
                            "description": (
                                'Optional short heading for the question, such as "Confirm" or "Choose Mode".'
                            ),
                        },
                        "options": {
                            "type": "array",
                            "description": (
                                "Optional choices to show the user. If you recommend one, put it "
                                'first and append "(Recommended)" to that label.'
                            ),
                            "items": {
                                "type": "object",
                                "additionalProperties": True,
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Short user-facing option label.",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": ("One sentence explaining the tradeoff or impact."),
                                    },
                                },
                                "required": ["label"],
                            },
                        },
                        "multi_select": {
                            "type": "boolean",
                            "description": (
                                "Whether the user may select more than one option. Defaults to false."
                            ),
                        },
                    },
                    "required": ["id", "question"],
                },
            }
        },
        "required": ["questions"],
    }

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
