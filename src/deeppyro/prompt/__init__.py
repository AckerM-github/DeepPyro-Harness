"""System prompt assembly."""

from __future__ import annotations

from deeppyro.prompt.projection import (
    RUNTIME_CONTEXT_CLEARED,
    RUNTIME_CONTEXT_SOURCE,
    ProjectionAction,
    ProjectionUpdate,
    RetainedSnapshot,
    RuntimeContextProjection,
    SystemPromptProjection,
    create_runtime_context_message,
    create_system_message,
)
from deeppyro.prompt.prompt import (
    DEFAULT_MAX_INSTRUCTION_CHARS,
    INSTRUCTION_FILENAME,
    PromptContext,
    build_system_prompt,
    collect_instructions,
)

__all__ = [
    "DEFAULT_MAX_INSTRUCTION_CHARS",
    "INSTRUCTION_FILENAME",
    "RUNTIME_CONTEXT_CLEARED",
    "RUNTIME_CONTEXT_SOURCE",
    "ProjectionAction",
    "ProjectionUpdate",
    "PromptContext",
    "RetainedSnapshot",
    "RuntimeContextProjection",
    "SystemPromptProjection",
    "build_system_prompt",
    "collect_instructions",
    "create_runtime_context_message",
    "create_system_message",
]
