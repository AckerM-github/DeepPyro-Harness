"""Runtime-context snapshot assembly.

The system prompt is stable, but some facts belong to the current moment:
working directory, model, permission preset and approval policy.  They are
injected as a user message whose source is "runtime-context"; the projection in
deeppyro.prompt.projection turns repeated identical snapshots into a no-op and
marks older snapshots as superseded when the values change.
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
    text = "\n".join(
        [
            "## Runtime",
            "",
            f"- Working directory: {ctx.cwd}",
            f"- Platform: {ctx.platform}",
            f"- Model: {ctx.model or 'unknown'}",
            f"- Session: {ctx.session_id}",
            f"- Permission preset: {ctx.permission_preset or 'unknown'}",
            f"- Approval policy: {ctx.approval_policy or 'unknown'}",
        ]
    )
    return [{"name": "runtime", "text": text}]


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
