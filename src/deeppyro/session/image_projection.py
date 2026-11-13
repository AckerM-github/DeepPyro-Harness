"""Pure projection for durable image/offload records (DSH image offload).

DSH keeps image bytes out of the session log: a user message stores only an
ImageAttachmentRef, and the provider-facing request may omit the oldest images
to fit a request image budget.  That decision is durable but not a surface
event: an image/offload record carries targets of the shape
{"seq": <message seq>, "imageIndexes": [<content index>, ...]} and a pure
interpreter rewrites the model-visible message at derivation time.  The same
durable log can therefore replay into different requests under different
providers or budgets.

The placeholder text is byte-for-byte DSH's no-access variant from
@deepseek-ai/dsh-llm/lib/index.js (imageIdentity / offloadedImageText called
without an access path).  See docs/spec/image-offload.md for the reverse-
engineered semantics and the deliberate differences.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any

from deeppyro.core.errors import SessionError
from deeppyro.core.wire import ContentPart, ImagePart, LogRecord, Message, TextPart

#: Byte-for-byte DSH prefix of the no-access offloaded-image placeholder.
OFFLOADED_IMAGE_PREFIX = "[image omitted to fit request image limits; "
#: Byte-for-byte DSH suffix of the no-access offloaded-image placeholder.
OFFLOADED_IMAGE_SUFFIX = (
    " No local normalized image path is available; ask the user to attach it again if needed.]"
)

#: The durable record type that carries offload decisions.
IMAGE_OFFLOAD_EVENT = "image/offload"


def image_identity(ref: Mapping[str, Any]) -> str:
    """Return the stable model-facing identity of one image attachment.

    DSH renders String(ref.attachmentId) when the occurrence carries no display
    name, and JSON.stringify(ref.name) + " (" + attachmentId + ")" otherwise
    (JSON double quotes, so embedded quotes are backslash-escaped).  Python's
    json.dumps matches JSON.stringify only with ensure_ascii=False: non-ASCII
    names must stay raw for byte parity.
    """
    attachment_id = ref.get("attachmentId")
    if attachment_id is None:
        raise SessionError("image attachment reference lacks attachmentId")
    name = ref.get("name")
    if not isinstance(name, str) or not name:
        return str(attachment_id)
    quoted = json.dumps(name, ensure_ascii=False)
    return f"{quoted} ({attachment_id})"


def offloaded_image_text(ref: Mapping[str, Any]) -> str:
    """Return DSH's exact no-access placeholder for one omitted image."""
    return f"{OFFLOADED_IMAGE_PREFIX}{image_identity(ref)}.{OFFLOADED_IMAGE_SUFFIX}"


def _attachment_ref(attachment: object) -> Mapping[str, Any]:
    """Normalize a wire attachment (ref object or bare id) to a mapping."""
    if isinstance(attachment, Mapping):
        return attachment
    # A bare attachment id (or any non-object) still has a stable identity.
    return {"attachmentId": str(attachment)}


def apply_image_offloads(message: Message, indexes: AbstractSet[int]) -> Message:
    """Return a copy of *message* with the selected image blocks replaced by text.

    Only ImagePart blocks whose content index is in *indexes* change; every
    other part keeps its position and value, and the input message is never
    mutated.  Indexes outside range(len(content)) and indexes that do not point
    at an image are ignored.
    """
    selected = set(indexes)
    content: list[ContentPart] = []
    for index, part in enumerate(message.content):
        if index in selected and isinstance(part, ImagePart):
            content.append(TextPart(text=offloaded_image_text(_attachment_ref(part.attachment))))
        else:
            content.append(part)
    return message.model_copy(update={"content": content})


def project_image_offloads(records: Sequence[LogRecord], *, strict: bool = True) -> dict[int, set[int]]:
    """Collect durable image-offload decisions keyed by surface record seq.

    Every image/offload record carries data.targets entries of the shape
    {"seq": <session seq>, "imageIndexes": [<content index>, ...]}.  Several
    records may name the same target seq; their index sets are unioned so the
    projection is idempotent.  Target seqs that are absent from the folded
    surface are simply never looked up by the caller.

    Strict mode rejects a malformed record with SessionError; tolerant mode
    skips that record while keeping the valid ones (trimmed fixture logs).
    """
    offloads: dict[int, set[int]] = {}
    for record in records:
        if record.type != IMAGE_OFFLOAD_EVENT:
            continue
        targets = _record_targets(record, strict=strict)
        if targets is None:
            continue
        for seq, indexes in targets:
            offloads.setdefault(seq, set()).update(indexes)
    return offloads


def _record_targets(record: LogRecord, *, strict: bool) -> list[tuple[int, list[int]]] | None:
    """Validate one image/offload payload; None means "skip in tolerant mode"."""
    targets = record.data.get("targets")
    if not isinstance(targets, list):
        return _invalid(record, "targets must be an array", strict=strict)
    parsed: list[tuple[int, list[int]]] = []
    for position, target in enumerate(targets):
        if not isinstance(target, Mapping):
            return _invalid(record, f"targets[{position}] must be an object", strict=strict)
        seq = target.get("seq")
        if not _is_index(seq):
            return _invalid(record, f"targets[{position}].seq must be a non-negative integer", strict=strict)
        indexes = target.get("imageIndexes")
        if not isinstance(indexes, list):
            return _invalid(record, f"targets[{position}].imageIndexes must be an array", strict=strict)
        for index_position, index in enumerate(indexes):
            if not _is_index(index):
                return _invalid(
                    record,
                    f"targets[{position}].imageIndexes[{index_position}] must be a non-negative integer",
                    strict=strict,
                )
        parsed.append((seq, list(indexes)))
    return parsed


def _invalid(record: LogRecord, detail: str, *, strict: bool) -> None:
    if strict:
        raise SessionError(f"invalid image/offload record at seq {record.seq}: {detail}")


def _is_index(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
