"""Default plugin set shipped with DeepPyro.

Each module provides one or more api.Plugin classes that the core bundle
(packaged at deeppyro/bundles/core/bundle.toml) mounts. Nothing here is
special: a user bundle can disable, replace or wrap any of these rows.
"""

from __future__ import annotations

from deeppyro.builtin.agents import AgentsPlugin
from deeppyro.builtin.commands import CommandsPlugin
from deeppyro.builtin.commands_builtin import CommandsBuiltinPlugin
from deeppyro.builtin.llm import LlmPlugin
from deeppyro.builtin.prompt_core import PromptCorePlugin
from deeppyro.builtin.prompt_service import PromptPlugin
from deeppyro.builtin.sandbox import SandboxPlugin
from deeppyro.builtin.sessions import SessionsPlugin
from deeppyro.builtin.tools import BuiltinToolsPlugin, ToolsPlugin

__all__ = [
    "AgentsPlugin",
    "BuiltinToolsPlugin",
    "CommandsBuiltinPlugin",
    "CommandsPlugin",
    "LlmPlugin",
    "PromptCorePlugin",
    "PromptPlugin",
    "SandboxPlugin",
    "SessionsPlugin",
    "ToolsPlugin",
]
