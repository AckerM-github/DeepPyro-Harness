"""Builtin ask-user plugin: provides the user_questions service.

The service is a stable seam between the ask_user_question tool and the host.
The TUI (or headless host) installs the answer handler; this plugin only
provides the service and registers the tool.
"""

from __future__ import annotations

from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.tools.builtin.ask_user import AskUserQuestionTool
from deeppyro.user_questions import UserQuestionsService


class UserQuestionsPlugin(Plugin):
    """Expose the user-questions service and the ask_user_question tool."""

    provide = "user_questions"
    plugin_id = "tool-ask-user"
    version = "0.1.0"
    description = "blocking ask_user_question service and its model-facing tool"
    inject: ClassVar[list[str]] = ["tools"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.service = UserQuestionsService()

    def setup(self, ctx: Any, config: Any) -> None:
        ctx.provide("user_questions", self.service)
        ctx.register_tool(lambda _cfg, _workspace: [AskUserQuestionTool()])


__all__ = ["UserQuestionsPlugin", "UserQuestionsService"]
