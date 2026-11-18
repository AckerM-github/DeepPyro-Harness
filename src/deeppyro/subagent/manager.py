"""Default subagent service: one-shot and forked child agents on native sessions.

A spawn runs one child agent to completion for a self-contained task.  A fork
starts from the parent session's last completed turn boundary and runs in the
background: the tool returns an activation immediately and the parent receives a
settlement notice when the child finishes.  A child is a native session with
origin "subagent" and delegation depth 1, so the native loop, trace and
checkpoint stores own every child run.

This module is the stable service facade the plugins and runtime bind to; the
behaviour lives in NativeSubagentService (:mod:deeppyro.subagent.native_manager).
The retired v4 session runtime is gone, so a v4 session handle is rejected at
bind time instead of being silently ignored.
"""

from __future__ import annotations

from typing import Any

from deeppyro.api.types import CHILD_DISABLED_TOOLS
from deeppyro.session.schema import NativeSession
from deeppyro.subagent.errors import SubagentError
from deeppyro.subagent.native_manager import NativeSubagentService
from deeppyro.subagent.types import MAX_LIVE_RUNS


def _is_native_parent(candidate: Any) -> bool:
    """Whether a bound session or agent belongs to the native runtime."""
    if isinstance(candidate, NativeSession):
        return True
    return isinstance(getattr(candidate, "native_session", None), NativeSession)


def filter_child_tools(registry: Any) -> Any:
    """Return a tool registry without the orchestration tools a child must not receive."""
    without = getattr(registry, "without", None)
    if callable(without):
        return without(CHILD_DISABLED_TOOLS)
    # Local fallback: the import stays local so the module does not depend on
    # deeppyro.tools being initialized first (tools.builtin imports the subagent
    # tool at runtime).
    from deeppyro.tools.registry import ToolRegistry

    return ToolRegistry([tool for tool in registry if getattr(tool, "name", "") not in CHILD_DISABLED_TOOLS])


class SubagentService(NativeSubagentService):
    """One-shot and background child agents for a configured parent session.

    The service is a thin native facade: session binding, child runs, lifecycle
    facts, measured usage and settlement notices are implemented by
    NativeSubagentService.  The sandbox is accepted because the plugin wiring
    still passes it; native hosts inherit the parent permission graph instead of
    a per-child sandbox handle.
    """

    def __init__(
        self,
        *,
        agents: Any = None,
        sessions: Any = None,
        sandbox: Any = None,
        max_runs: int = MAX_LIVE_RUNS,
    ) -> None:
        super().__init__(agents=agents, sessions=sessions, max_runs=max_runs)
        self._sandbox = sandbox

    @property
    def native(self) -> SubagentService:
        """The native implementation, kept as a read-only alias for call sites."""
        return self

    def bind_session(self, session: Any) -> None:
        """Bind the parent native session that receives the lifecycle facts."""
        if session is not None and not _is_native_parent(session):
            raise SubagentError("subagent service requires a native session")
        super().bind_session(session)


__all__ = ["SubagentService", "filter_child_tools"]
