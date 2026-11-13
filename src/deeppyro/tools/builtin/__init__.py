"""Built-in tool set: filesystem, search, shell, web, todo, present."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deeppyro.core.protocols import Tool
from deeppyro.tools.builtin.ask_user import AskUserQuestionTool
from deeppyro.tools.builtin.fs import EditTool, ReadTool, WriteTool
from deeppyro.tools.builtin.present import PresentTool
from deeppyro.tools.builtin.search import GlobTool, GrepTool
from deeppyro.tools.builtin.shell import BashTool, PwshTool
from deeppyro.tools.builtin.todo import TodoWriteTool
from deeppyro.tools.builtin.web import WebFetchTool, WebSearchTool
from deeppyro.tools.builtin.working_directory import WorkingDirectoryTool


def _cfg_value(cfg: Any, key: str, default: Any) -> Any:
    """Read an optional `[tools]` override from the config model."""
    tools_cfg = getattr(cfg, "tools", None)
    if tools_cfg is None:
        return default
    if isinstance(tools_cfg, dict):
        value = tools_cfg.get(key)
    else:
        value = getattr(tools_cfg, key, None)
    return default if value is None else value


def builtin_tools(cfg: Any, workspace: Path) -> list[Tool]:
    """Return the default tool list for a session rooted at `workspace`.

    Optional per-tool limits can be overridden through a `[tools]` section in
    the config file (`globMaxResults`, `grepMaxMatches`, `webSearchMaxResults`,
    `webFetchMaxChars`, `presentMaxFiles`); everything else uses the DSH
    defaults. The workspace is not baked into the tools - every call resolves
    paths against `ToolContext.cwd` so a session can change directory.
    """
    # Workspace is accepted for interface symmetry and future per-workspace
    # configuration; tools resolve against the live ToolContext instead.
    _ = Path(workspace)
    return [
        ReadTool(),
        WriteTool(),
        EditTool(),
        GlobTool(max_results=int(_cfg_value(cfg, "globMaxResults", 100))),
        GrepTool(max_matches=int(_cfg_value(cfg, "grepMaxMatches", 250))),
        PwshTool(),
        BashTool(),
        WebSearchTool(max_results=int(_cfg_value(cfg, "webSearchMaxResults", 8))),
        WebFetchTool(max_output_chars=int(_cfg_value(cfg, "webFetchMaxChars", 200_000))),
        TodoWriteTool(),
        PresentTool(max_files=int(_cfg_value(cfg, "presentMaxFiles", 8))),
        WorkingDirectoryTool(),
        AskUserQuestionTool(),
    ]


__all__ = [
    "AskUserQuestionTool",
    "BashTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "PresentTool",
    "PwshTool",
    "ReadTool",
    "TodoWriteTool",
    "WebFetchTool",
    "WebSearchTool",
    "WorkingDirectoryTool",
    "WriteTool",
    "builtin_tools",
]
