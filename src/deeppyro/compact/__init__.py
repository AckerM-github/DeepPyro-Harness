"""Context compaction."""

from __future__ import annotations

from deeppyro.compact.compactor import Compactor
from deeppyro.compact.prompts import (
    CHECKPOINT_PREAMBLE,
    COMPACTION_INSTRUCTION,
    SUMMARY_CLOSE_TAG,
    SUMMARY_OPEN_TAG,
    frame_summary,
)
from deeppyro.compact.surface import (
    CompactionRegion,
    EntryState,
    SurfaceNode,
    balanced_after,
    balanced_before,
    derive_node_message,
    entry_state,
    fold_surface,
    latest_tool_schemas,
    select_region,
    surface_messages,
    tool_pairing_cuts,
)
from deeppyro.compact.tokens import (
    CHARS_PER_TOKEN,
    MESSAGE_OVERHEAD,
    estimate_message_tokens,
    estimate_messages_tokens,
    estimate_text_tokens,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "CHECKPOINT_PREAMBLE",
    "COMPACTION_INSTRUCTION",
    "MESSAGE_OVERHEAD",
    "SUMMARY_CLOSE_TAG",
    "SUMMARY_OPEN_TAG",
    "CompactionRegion",
    "Compactor",
    "EntryState",
    "SurfaceNode",
    "balanced_after",
    "balanced_before",
    "derive_node_message",
    "entry_state",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "estimate_text_tokens",
    "fold_surface",
    "frame_summary",
    "latest_tool_schemas",
    "select_region",
    "surface_messages",
    "tool_pairing_cuts",
]
