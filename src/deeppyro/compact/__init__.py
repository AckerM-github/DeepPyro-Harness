"""Context compaction: blocks, refs and the native compactor."""

from __future__ import annotations

from deeppyro.compact.blocks import BlockRecord, BlockStore, CompactError
from deeppyro.compact.native_compactor import (
    CompactionDone,
    CompactionRegion,
    NativeCompactor,
    select_region,
    should_compact_before_send,
    tool_safe_cuts,
)
from deeppyro.compact.native_compress import (
    CompressBlock,
    CompressedRange,
    NativeCompressor,
    NativeCompressOutcome,
    parse_content,
    requested_refs,
)
from deeppyro.compact.native_refs import (
    RefEntry,
    RefLocation,
    RefPlan,
    RefStore,
    canonical_ref,
    resolve_refs,
)
from deeppyro.compact.prompts import (
    CHECKPOINT_PREAMBLE,
    COMPACTION_INSTRUCTION,
    SUMMARY_CLOSE_TAG,
    SUMMARY_OPEN_TAG,
    frame_summary,
)

__all__ = [
    "CHECKPOINT_PREAMBLE",
    "COMPACTION_INSTRUCTION",
    "SUMMARY_CLOSE_TAG",
    "SUMMARY_OPEN_TAG",
    "BlockRecord",
    "BlockStore",
    "CompactError",
    "CompactionDone",
    "CompactionRegion",
    "CompressBlock",
    "CompressedRange",
    "NativeCompactor",
    "NativeCompressOutcome",
    "NativeCompressor",
    "RefEntry",
    "RefLocation",
    "RefPlan",
    "RefStore",
    "canonical_ref",
    "frame_summary",
    "parse_content",
    "requested_refs",
    "resolve_refs",
    "select_region",
    "should_compact_before_send",
    "tool_safe_cuts",
]
