"""Builtin hook journal: write hook dispatches into the executing agent session.

The plugin observes every HookService dispatch and records hook/invoked and
hook/result into the AgentSession that owns the payload. The session may emit a
live event for each record, which gives headless NDJSON and the TUI a decision
chain without subscribing to every hook individually.

Hot hooks (llm/stream and agent/assistant-stream) are skipped because they run
once per model frame, and dispatches without an agent in the payload have no
session to write to. Record failures are logged and isolated so journaling can
never change a hook result.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from deeppyro.api import Plugin
from deeppyro.hooks.records import (
    INVOKED_RECORD_TYPE,
    RESULT_RECORD_TYPE,
    agent_from_payload,
    invoked_record,
    is_journaled,
    result_record,
)
from deeppyro.hooks.service import HookObservation

logger = logging.getLogger(__name__)


class HookJournalPlugin(Plugin):
    """Persist hook dispatch lifecycles into the session of the dispatching agent."""

    plugin_id = "hook-journal"
    version = "0.1.0"
    description = "journal hook dispatches into the executing agent session log"
    inject: ClassVar[list[str]] = ["hooks"]

    def __init__(self, ctx: Any, config: Any) -> None:
        super().__init__(ctx, config)
        self.records_written = 0

    def setup(self, ctx: Any, config: Any) -> None:
        hooks = ctx.service("hooks")
        ctx.effect(lambda: hooks.add_observer(self._observe), label="hook-journal")

    def _observe(self, observation: HookObservation) -> None:
        """Record one observation, skipping hot hooks and payloads without an agent."""
        if not is_journaled(observation.hook):
            return
        agent = agent_from_payload(observation.payload)
        if agent is None:
            return
        if observation.kind == "invoked":
            record_type = INVOKED_RECORD_TYPE
            record = invoked_record(observation)
        elif observation.kind == "resolved":
            record_type = RESULT_RECORD_TYPE
            record = result_record(observation)
        else:
            return
        try:
            agent.record_hook_event(record_type, record)
        except Exception:
            logger.exception("hook journal failed to record %r", observation.hook)
            return
        self.records_written += 1


__all__ = ["HookJournalPlugin"]
