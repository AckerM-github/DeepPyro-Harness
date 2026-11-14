"""Runtime-context snapshot assembly.

The system prompt is stable, but some facts belong to the current moment:
working directory, model, sandbox file policy and approval policy.  They are
injected as a user message whose source is "runtime-context"; the projection in
deeppyro.prompt.projection turns repeated identical snapshots into a no-op and
marks older snapshots as superseded when the values change.

The sandbox and approval paragraphs mirror the reference runtime-context
contributions, so the model is told the standing policy and the escalation
rules without freezing them into the system prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from deeppyro.prompt.prompt import PromptContext

RUNTIME_CONTEXT_HEADER = (
    "Current runtime context. This snapshot supersedes earlier runtime-context snapshots."
)

CLEARED_RUNTIME_CONTEXT = "Current runtime context: none. Earlier runtime-context snapshots no longer apply."


def runtime_context_sections(ctx: PromptContext) -> list[dict[str, Any]]:
    """Return the named sections that make up this runtime snapshot."""
    runtime = "\n".join(
        [
            "## Runtime",
            "",
            f"- Working directory: {ctx.cwd}",
            f"- Platform: {ctx.platform}",
            f"- Model: {ctx.model or 'unknown'}",
            f"- Session: {ctx.session_id}",
        ]
    )
    sections: list[dict[str, Any]] = [{"name": "runtime", "text": runtime}]
    if ctx.sandbox_policy_text.strip():
        sections.append({"name": "sandbox:policy", "text": ctx.sandbox_policy_text.strip()})
    if ctx.approval_policy_text.strip():
        sections.append({"name": "approval:policy", "text": ctx.approval_policy_text.strip()})
    return sections


def render_runtime_context(sections: Sequence[dict[str, Any]]) -> str:
    """Render sections into the text stored on the runtime-context message."""
    parts: list[str] = []
    for section in sections:
        text = str(section.get("text") or "").strip()
        if text:
            parts.append(text)
    if not parts:
        return ""
    return RUNTIME_CONTEXT_HEADER + "\n\n" + "\n\n".join(parts)


__all__ = [
    "CLEARED_RUNTIME_CONTEXT",
    "RUNTIME_CONTEXT_HEADER",
    "render_runtime_context",
    "runtime_context_sections",
]
