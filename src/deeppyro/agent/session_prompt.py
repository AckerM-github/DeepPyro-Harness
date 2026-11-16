"""System-prompt and projection bookkeeping for the agent session."""

from __future__ import annotations

import sys
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.ids import now_ms
from deeppyro.hooks.types import SYSTEM_PROMPT_ASSEMBLE
from deeppyro.prompt import PromptContext
from deeppyro.prompt.projection import ProjectionUpdate, RuntimeContextProjection, SystemPromptProjection
from deeppyro.prompt.runtime_context import render_runtime_context, runtime_context_sections
from deeppyro.sandbox.policy import approval_policy_text, sandbox_policy_text

from .session_records import RecordLayer
from .session_support import logger


class PromptLayer(RecordLayer):
    """System-prompt and projection bookkeeping for the agent session."""

    def _prompt_context(self) -> PromptContext:
        started_at = self._session.header.createdAt
        if not isinstance(started_at, int) or isinstance(started_at, bool):
            started_at = now_ms()
        return PromptContext(
            cwd=self._current_cwd(),
            platform=sys.platform,
            session_id=self._session.session_id,
            started_at=started_at,
            permission_preset=self._permission_preset(),
            approval_policy=self._approval_policy(),
            model=self._model,
            sandbox_policy_text=self._sandbox_policy_text(),
            approval_policy_text=self._approval_policy_text(),
            instructions=list(self._instructions),
            env={},
            direct_tool_names=tuple(self._surface.names()),
        )

    async def _commit_system_prompt(self, turn: int, step: int, *, starts_series: bool) -> None:
        records = list(self._records())
        schemas = self._registry.schemas()
        rendered = self._prompt_builder(schemas, self._prompt_context())
        if not isinstance(rendered, str):
            raise SessionError(f"prompt builder returned {type(rendered).__name__}, expected str")
        rendered = await self._assemble_system_prompt(turn, step, rendered, schemas)
        projection = SystemPromptProjection(records)
        for update in projection.project(rendered, in_history=True, starts_series=starts_series):
            self._commit_projection("system/message", update, turn, step)

    async def _assemble_system_prompt(self, turn: int, step: int, rendered: str, schemas: list[Any]) -> str:
        """Let system-prompt/assemble rewrite the rendered prompt text.

        The waterfall fallback returns the prompt unchanged, so listeners that
        only observe do not affect the result.  A non-string result is a
        contract violation: the projection persists text, not a structure.
        """
        if not self._hook_enabled(SYSTEM_PROMPT_ASSEMBLE):
            return rendered
        hooks = self._hooks
        assert hooks is not None
        payload = {
            "agent": self,
            "turn": turn,
            "step": step,
            "prompt": rendered,
            "tools": [schema.model_dump() for schema in schemas],
            "signal": None,
        }
        result = await hooks.waterfall(
            SYSTEM_PROMPT_ASSEMBLE, payload, fallback=lambda: rendered, scope=self._hook_scope()
        )
        if result is None:
            # A listener that vetoes the dispatch suppresses the rewrite; the
            # built-in rendered prompt stays in place.
            return rendered
        if not isinstance(result, str):
            raise SessionError(f"system-prompt/assemble hook returned {type(result).__name__}, expected str")
        return result

    def _commit_runtime_context(self, turn: int, step: int) -> None:
        records = list(self._records())
        context = self._prompt_context()
        sections = runtime_context_sections(context)
        rendered = render_runtime_context(sections)
        projection = RuntimeContextProjection(records)
        update = projection.project(rendered, sections)
        if update is not None:
            self._commit_projection("user/message", update, turn, step)

    def _commit_projection(self, record_type: str, update: ProjectionUpdate, turn: int, step: int) -> None:
        message = update.message
        if message is None:
            raise SessionError(f"{record_type} projection produced no message")
        dump = message.model_dump(exclude_none=True)
        if record_type == "user/message":
            data: dict[str, Any] = dump
        else:
            data = {"turn": turn, "step": step, "message": dump}
        if update.kind == "append":
            self._append(record_type, data, surfaceOp="append")
            return
        if update.kind != "replace":
            raise SessionError(f"{record_type} projection produced unsupported kind {update.kind!r}")
        seqs = sorted({seq for seq, _ in update.replace})
        if not seqs:
            raise SessionError(f"{record_type} replace projection has no target records")
        self._append(
            record_type,
            data,
            surfaceOp={"op": "replace", "startSeq": seqs[0], "endSeq": seqs[-1]},
            sourceEventSeqs=seqs,
        )

    def _permission_preset(self) -> str:
        if self._options.permission_preset:
            return self._options.permission_preset
        return str(getattr(self._permission, "preset", "") or "workspace-write")

    def _approval_policy(self) -> str:
        if self._options.approval_policy:
            return self._options.approval_policy
        return str(getattr(self._permission, "approval", "") or "ask")

    def _sandbox_policy_text(self) -> str:
        """The DSH runtime-context paragraph for the standing file policy."""
        policy = getattr(self._permission, "policy", None)
        if policy is None:
            return ""
        try:
            return sandbox_policy_text(policy)
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - defensive for custom gates
            logger.warning("could not render the sandbox policy text", exc_info=True)
            return ""

    def _approval_policy_text(self) -> str:
        """The DSH runtime-context paragraph for the standing approval policy."""
        return approval_policy_text(self._approval_policy())
