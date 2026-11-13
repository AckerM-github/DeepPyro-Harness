"""Provider-request image planning for the agent loop.

The durable session log never stores image bytes: message content carries only
an attachment reference, and the image/offload projection replaces the oldest
occurrences with placeholder text when messages are derived.  A provider request
needs the opposite direction, so the loop asks this module for one plan per step:

1. fold the surface and apply the existing offload projection;
2. resolve the remaining attachment references to inline data URLs in memory;
3. measure every occurrence and, when the route budget is exceeded, select the
   oldest occurrences that have to be offloaded and return the updated messages
   plus the targets of a new durable image/offload record.

Only the messages returned here ever contain base64.  The caller persists just
the offload targets, so the same log replays under a different route budget, and
the durable log stays free of image bytes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from deeppyro.core.wire import ImagePart, LogRecord, Message
from deeppyro.llm.image_budget import ImageBudget, base64_length, required_image_offload
from deeppyro.llm.messages import image_part_url
from deeppyro.session.derive import event_message, fold_surface
from deeppyro.session.image_projection import apply_image_offloads, project_image_offloads

__all__ = [
    "ImageRequestPlan",
    "expand_image_attachments",
    "plan_request_images",
    "surface_messages_with_seq",
]

_DATA_URL_PREFIX = "data:"

#: Prefixes of attachments that already are a ready wire representation.
_URL_PREFIXES = ("data:", "http://", "https://")


@dataclass(frozen=True, slots=True)
class ImageRequestPlan:
    """The messages a provider request should carry plus the offload decision.

    * messages: the model-visible history for this request, with attachment
      references already expanded to inline data URLs and the selected oldest
      images replaced by the offload placeholder;
    * offload_targets: durable image/offload targets ({"seq", "imageIndexes"})
      that the caller must append before building the request, empty when the
      request fits the budget;
    * offloaded: how many image occurrences the targets cover.
    """

    messages: list[Message]
    offload_targets: list[dict[str, Any]] = field(default_factory=list)
    offloaded: int = 0

    def record_data(self) -> dict[str, Any]:
        """Return the data payload for a durable image/offload record.

        Empty when nothing needs to be offloaded, so the caller can use the
        result to decide whether appending a record is necessary at all.
        """
        if not self.offload_targets:
            return {}
        targets: list[dict[str, Any]] = []
        for target in self.offload_targets:
            indexes = [int(index) for index in target["imageIndexes"]]
            targets.append({"seq": int(target["seq"]), "imageIndexes": indexes})
        return {"targets": targets}


def surface_messages_with_seq(
    records: Sequence[LogRecord], *, offloads: Mapping[int, set[int]] | None = None
) -> list[tuple[int, Message]]:
    """Fold the surface into (record seq, message) pairs in model order.

    Each surface node keeps its durable seq so a later budget decision can name
    the node it targets.  Nodes that produce no message (empty system/developer/
    assistant placeholders) are skipped.  The image/offload projection is applied
    on top of the fold: either the caller supplies already collected *offloads*,
    or they are read from *records* here.
    """
    nodes = fold_surface(records)
    applied = dict(offloads) if offloads is not None else project_image_offloads(records)
    entries: list[tuple[int, Message]] = []
    for node in nodes:
        message = event_message(node)
        if message is None:
            continue
        indexes = applied.get(node.seq)
        if indexes:
            message = apply_image_offloads(message, indexes)
        entries.append((node.seq, message))
    return entries


def expand_image_attachments(messages: Sequence[Message], attachments: Any) -> list[Message]:
    """Return copies of *messages* with resolvable references inlined as data URLs.

    A part whose attachment is already a data:/http(s) URL (bare string or a
    mapping with url/base64 data) is left untouched.  Every other reference is
    resolved through :func:`deeppyro.llm.messages.image_part_url` with the
    injected resolver; unresolvable references stay unchanged so the caller can
    still derive and offload them.  Inputs are never mutated, and duplicate
    occurrences of one reference are resolved (bytes read) once per call.
    """
    resolver = _AttachmentResolver(attachments)
    cache: dict[tuple[Any, ...], str | None] = {}
    expanded: list[Message] = []
    for message in messages:
        content = list(message.content)
        for index, part in enumerate(content):
            if not isinstance(part, ImagePart) or not _needs_expansion(part.attachment):
                continue
            key = _cache_key(part.attachment)
            if key not in cache:
                try:
                    cache[key] = image_part_url(part, attachments=resolver)
                except Exception:  # noqa: BLE001 - an unresolvable ref stays a ref
                    cache[key] = None
            url = cache[key]
            if url is None:
                continue
            content[index] = part.model_copy(update={"attachment": {"url": url}})
        expanded.append(message.model_copy(update={"content": content}))
    return expanded


def plan_request_images(
    records: Sequence[LogRecord], *, attachments: Any, budget: ImageBudget | None = None
) -> ImageRequestPlan:
    """Plan one provider request from the durable log.

    Without a usable attachment service (None, or one exposing neither
    read_image_bytes nor image_host_path) this degrades to the pre-offload
    behaviour: the derived messages are returned unchanged and nothing is
    written.  Otherwise the remaining references are expanded in memory, the
    represented inline size is measured, and the oldest occurrences are selected
    until the route budget fits.

    A selected occurrence becomes a target for the caller to append as one
    image/offload record; the returned messages already carry the placeholder,
    so the request that follows the append is consistent with the durable log.
    """
    entries = surface_messages_with_seq(records)
    messages = [message for _, message in entries]
    if not _can_read_images(attachments):
        return ImageRequestPlan(messages=messages)

    expanded = expand_image_attachments(messages, attachments)
    planned_entries = list(zip((seq for seq, _ in entries), expanded, strict=True))
    lengths = {
        id(part): _inline_length(part)
        for _, message in planned_entries
        for part in message.content
        if isinstance(part, ImagePart)
    }
    if not lengths:
        return ImageRequestPlan(messages=expanded)

    active_budget = budget if budget is not None else ImageBudget()
    required = required_image_offload(
        expanded,
        active_budget,
        version_bytes=lambda part: lengths[id(part)],
    )
    if required <= 0:
        return ImageRequestPlan(messages=expanded)

    targets = _select_offload_targets(planned_entries, required)
    if not targets:
        return ImageRequestPlan(messages=expanded)

    originals = dict(entries)
    final_messages = [
        _merge_offload_text(originals[seq], message, set(targets[seq])) if seq in targets else message
        for seq, message in planned_entries
    ]
    return ImageRequestPlan(
        messages=final_messages,
        offload_targets=[{"seq": seq, "imageIndexes": list(indexes)} for seq, indexes in targets.items()],
        offloaded=sum(len(indexes) for indexes in targets.values()),
    )


def _merge_offload_text(unexpanded: Message, expanded: Message, indexes: set[int]) -> Message:
    """Replace selected occurrences of *expanded* with the durable placeholder text.

    A placeholder names the durable attachment reference (attachmentId, optional
    display name), so it is built from the unexpanded message; every other part --
    including images that stay inline -- comes from the expanded message.
    """
    placeholders = apply_image_offloads(unexpanded, indexes)
    content = list(expanded.content)
    for index in indexes:
        if 0 <= index < len(placeholders.content):
            content[index] = placeholders.content[index]
    return expanded.model_copy(update={"content": content})


def _select_offload_targets(entries: Sequence[tuple[int, Message]], required: int) -> dict[int, list[int]]:
    """Pick the oldest *required* image occurrences, grouped by record seq."""
    targets: dict[int, list[int]] = {}
    remaining = required
    for seq, message in entries:
        if remaining <= 0:
            break
        for index, part in enumerate(message.content):
            if remaining <= 0:
                break
            if isinstance(part, ImagePart):
                targets.setdefault(seq, []).append(index)
                remaining -= 1
    return targets


def _inline_length(part: ImagePart) -> int:
    """Represented inline length (base64 characters) of one image occurrence."""
    attachment = part.attachment
    if isinstance(attachment, str):
        return _url_inline_length(attachment)
    if isinstance(attachment, Mapping):
        url = attachment.get("url")
        if isinstance(url, str) and url:
            return _url_inline_length(url)
        data = attachment.get("data") or attachment.get("base64")
        if isinstance(data, str) and data:
            return len(data)
        declared = _declared_bytes(attachment)
        return base64_length(declared) if declared is not None else 0
    return 0


def _url_inline_length(url: str) -> int:
    if not url.startswith(_DATA_URL_PREFIX):
        # An http(s) URL is not sent inline; it costs nothing on this route.
        return 0
    _, _, payload = url.partition(",")
    return len(payload)


def _declared_bytes(attachment: Mapping[str, Any]) -> int | None:
    value = attachment.get("bytes")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _needs_expansion(attachment: Any) -> bool:
    """Whether an attachment is a reference rather than a ready wire value."""
    if isinstance(attachment, str):
        return not attachment.startswith(_URL_PREFIXES)
    if isinstance(attachment, Mapping):
        url = attachment.get("url")
        if isinstance(url, str) and url:
            return False
        data = attachment.get("data") or attachment.get("base64")
        return not (isinstance(data, str) and data)
    return False


def _cache_key(attachment: Any) -> tuple[Any, ...]:
    if isinstance(attachment, str):
        return ("id", attachment)
    if isinstance(attachment, Mapping):
        attachment_id = attachment.get("attachmentId")
        media_type = attachment.get("mediaType") or attachment.get("mimeType")
        if isinstance(attachment_id, str) and attachment_id:
            return ("ref", attachment_id, media_type)
        return ("ref-json", json.dumps(attachment, sort_keys=True, default=str))
    return ("other", repr(attachment))


def _can_read_images(attachments: Any) -> bool:
    if attachments is None:
        return False
    return callable(getattr(attachments, "read_image_bytes", None)) or callable(
        getattr(attachments, "image_host_path", None)
    )


class _AttachmentResolver:
    """Adapt the attachment service to the llm.messages resolver contract.

    The contract hands read_image_bytes/image_host_path whatever object the wire
    stored: a bare attachment id or the reference mapping from the session log.
    The attachment store keeps typed ImageAttachmentRef values, so a plain
    mapping is first resolved through lookup() and retried; resolvers that already
    accept wire-shaped references keep receiving exactly that object.  Methods the
    underlying service does not expose stay absent (None), so image_part_url can
    fall back from read_image_bytes to image_host_path.
    """

    __slots__ = ("image_host_path", "lookup", "read_image_bytes")

    def __init__(self, attachments: Any) -> None:
        reader = getattr(attachments, "read_image_bytes", None)
        self.read_image_bytes = _wrap_ref_callable(attachments, reader) if callable(reader) else None
        host_path = getattr(attachments, "image_host_path", None)
        self.image_host_path = _wrap_ref_callable(attachments, host_path) if callable(host_path) else None
        lookup = getattr(attachments, "lookup", None)
        self.lookup = lookup if callable(lookup) else None


def _wrap_ref_callable(attachments: Any, target: Any) -> Any:
    """Call *target* with the wire ref, retrying once with its typed reference."""

    def call(ref: Any) -> Any:
        try:
            return target(ref)
        except Exception:
            resolved = _typed_reference(attachments, ref)
            if resolved is None:
                raise
            return target(resolved)

    return call


def _typed_reference(attachments: Any, ref: Any) -> Any | None:
    attachment_id = _attachment_id(ref)
    if attachment_id is None:
        return None
    lookup = getattr(attachments, "lookup", None)
    if not callable(lookup):
        return None
    try:
        resolved = lookup(attachment_id)
    except Exception:  # noqa: BLE001 - a failed lookup leaves the ref unresolved
        return None
    return resolved if resolved is not None and resolved is not ref else None


def _attachment_id(ref: Any) -> str | None:
    if isinstance(ref, str):
        return ref or None
    if isinstance(ref, Mapping):
        value = ref.get("attachmentId")
        return value if isinstance(value, str) and value else None
    value = getattr(ref, "attachmentId", None)
    return value if isinstance(value, str) and value else None
