"""Shared dataclass state for the TUI runtime layers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deeppyro.api import DEFAULT_PRESET_ID
from deeppyro.config import Config


@dataclass
class _RuntimeState:
    """Shared dataclass state for the TUI runtime layers."""

    cfg: Config
    cwd: Path
    kernel_app: Any
    provider_name: str
    model: str
    context_window: int
    store: Any
    provider: Any
    registry: Any
    permission: Any
    agents: Any
    sink: Any
    ask: Callable[[Any], Awaitable[bool]]
    activity: Any = None
    jobs: Any = None
    team: Any = None
    subagent: Any = None
    working_directory: Any = None
    user_questions: Any = None
    projections: Any = None
    schedule: Any = None
    schedule_notice: Callable[[str], None] | None = None
    llm: Any = None
    agent_preset: str = DEFAULT_PRESET_ID
    preset_revision: int | None = None
    team_previous_preset: str = DEFAULT_PRESET_ID
    session: Any = None
    agent: Any = None
    last_persist_error: str | None = None
    last_catalog_error: str | None = None
    _catalog: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    _schedule_tasks: set[asyncio.Task[Any]] = field(default_factory=set, repr=False)
    _schedule_once: dict[str, str] = field(default_factory=dict, repr=False)
    _created_sessions: set[str] = field(default_factory=set, repr=False)
    _restored_team_sessions: set[str] = field(default_factory=set, repr=False)
    _closed: bool = field(default=False, repr=False)
