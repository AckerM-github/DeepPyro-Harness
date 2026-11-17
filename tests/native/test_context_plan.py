"""Unit tests for the pure M2 ContextPlan builder."""

from __future__ import annotations

from typing import Any

import pytest

from deeppyro.agent.context_plan import (
    ContextPlan,
    build_context_plan,
    estimate_message_tokens,
    estimate_text_tokens,
)
from deeppyro.core.ids import new_message_id
from deeppyro.core.messages import (
    ImagePart,
    Message,
    ModelSource,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    UserSource,
)
from deeppyro.core.protocols import ToolContext, ToolResult


def _user(text: str) -> Message:
    return Message(
        id=new_message_id(),
        role="user",
        content=(TextPart(text=text),),
        created_at=1,
        source=UserSource(),
    )


def _assistant(*parts: Any) -> Message:
    return Message(
        id=new_message_id(),
        role="assistant",
        content=tuple(parts),
        created_at=2,
        source=ModelSource(provider="fake", model="fake-model"),
    )


class FakeTool:
    def __init__(self, name: str = "echo", *, parallel_safe: bool = True) -> None:
        self.name = name
        self.description = "Echo a value"
        self.parallel_safe = parallel_safe
        self.parameters: dict[str, Any] = {"type": "object", "properties": {"value": {"type": "string"}}}
        self.output_schema: dict[str, Any] | None = {"type": "object"}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(text=str(args))


def test_text_token_estimation_rounds_up() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("a") == 1
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("abcde") == 2


def test_message_token_estimation_counts_parts() -> None:
    text_only = _user("abcd")
    assert estimate_message_tokens(text_only) == 5
    assistant = _assistant(
        ReasoningPart(text="abcd"),
        TextPart(text="abcd"),
        ToolCallPart(id="call_0123456789abcdef", name="echo", arguments="{}"),
    )
    assert estimate_message_tokens(assistant) == 4 + 1 + 1 + 1 + 1
    image = Message(
        id=new_message_id(),
        role="user",
        content=(ImagePart(attachment="attachment-1", media_type="image/png"),),
        created_at=3,
        source=UserSource(),
    )
    assert estimate_message_tokens(image) == 4 + 256


def test_plan_is_deterministic_for_equal_inputs() -> None:
    messages = [_user("hello"), _assistant(TextPart(text="hi"))]
    first = build_context_plan(messages)
    second = build_context_plan(list(messages))
    assert first == second
    assert isinstance(first.messages, tuple)
    assert first.source_message_ids == tuple(message.id for message in messages)
    assert first.estimated_tokens == sum(estimate_message_tokens(message) for message in messages)


def test_plan_includes_system_and_tool_schemas_in_order() -> None:
    system = _user("system prompt")
    messages = [_user("hello")]
    tools = [FakeTool("alpha"), FakeTool("beta")]
    plan = build_context_plan(messages, system=system, tools=tools)
    assert plan.system is system
    assert plan.tool_names == ("alpha", "beta")
    assert plan.source_message_ids == (system.id, messages[0].id)
    assert plan.estimated_tokens > sum(estimate_message_tokens(message) for message in messages)
    assert plan.tools[0].description == "Echo a value"
    assert plan.tools[0].parameters == tools[0].parameters
    assert plan.tools[0].output == {"type": "object"}
    assert plan.empty is False


def test_plan_is_isolated_from_later_tool_mutation() -> None:
    tool = FakeTool()
    plan = build_context_plan([_user("hello")], tools=[tool])
    tool.parameters["properties"]["value"] = {"type": "integer"}
    assert plan.tools[0].parameters == {"type": "object", "properties": {"value": {"type": "string"}}}


def test_plan_validation_rejects_bad_fields() -> None:
    message = _user("hello")
    with pytest.raises(ValueError, match="system"):
        ContextPlan(system="nope", messages=(message,), tools=(), estimated_tokens=0, source_message_ids=())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="messages"):
        ContextPlan(system=None, messages=[message], tools=(), estimated_tokens=0, source_message_ids=())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="estimated_tokens"):
        ContextPlan(
            system=None, messages=(message,), tools=(), estimated_tokens=-1, source_message_ids=(message.id,)
        )
    with pytest.raises(ValueError, match="source_message_ids"):
        ContextPlan(system=None, messages=(message,), tools=(), estimated_tokens=0, source_message_ids=())
    with pytest.raises(ValueError, match="tools"):
        ContextPlan(
            system=None,
            messages=(message,),
            tools=("nope",),  # type: ignore[arg-type]
            estimated_tokens=0,
            source_message_ids=(message.id,),
        )
