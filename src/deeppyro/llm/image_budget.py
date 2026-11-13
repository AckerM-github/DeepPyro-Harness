"""Provider-side accounting for request images and image offloading.

DSH keeps the durable log free of image bytes: a message carries an
`ImageAttachmentRef` (attachment id, media type, byte size, dimensions) and the
attachment store keeps the actual bytes. A provider request, however, has a
budget per route: a maximum number of image occurrences and a maximum number of
represented bytes. Once a request exceeds the route budget, the oldest retained
occurrences are replaced by the offload placeholder (see
`deeppyro.session.image_projection`) instead of being silently dropped.

This module ports the pure parts of that policy from DSH
`dsh-llm/lib/index.js:647-649` (:base64Length), `:692-709`
(:offloadedImagePrefixCount) and `:720-728` (:requiredImageOffload).  It depends
only on `deeppyro.core.wire` and never imports the attachment store: the caller
supplies the exact represented byte length of one occurrence through
`version_bytes`, exactly like DSH's `versionBytes` callback.

Budget semantics (P1 is inline/base64 only; the files route is modelled because
the DSH constants are a single configuration surface):

* `max_images` / `max_bytes` -- raw request limits (DSH `maxImagesPerRequest`,
  `maxRequestFilesBytes`).
* `max_inline_bytes` -- limit of the accumulated base64 payload when images are
  sent inline (DSH `maxInlineRequestImageBytes`).
* `offload_byte_quantum` / `inline_offload_byte_quantum` / `offload_count_quantum`
  -- deterministic removal steps, so two process runs with the same history
  offload exactly the same prefix.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from deeppyro.core.wire import ImagePart, Message

# DSH dsh-llm-deepseek/lib/types/defaults.js
DEFAULT_MAX_IMAGES = 600
DEFAULT_MAX_BYTES = 134_217_728
DEFAULT_MAX_INLINE_BYTES = 20_971_520
DEFAULT_OFFLOAD_BYTE_QUANTUM = 67_108_864
DEFAULT_INLINE_OFFLOAD_BYTE_QUANTUM = 10_485_760
DEFAULT_OFFLOAD_COUNT_QUANTUM = 20


@dataclass(frozen=True, slots=True)
class ImageBudget:
    """One route's image limits and deterministic removal quanta.

    The three quanta must be at least one and must not exceed the limit they
    step through, mirroring the DSH configuration checks (a removal step larger
    than the budget itself would make the first offload overshoot every time).
    """

    max_images: int = DEFAULT_MAX_IMAGES
    max_bytes: int = DEFAULT_MAX_BYTES
    max_inline_bytes: int = DEFAULT_MAX_INLINE_BYTES
    offload_byte_quantum: int = DEFAULT_OFFLOAD_BYTE_QUANTUM
    inline_offload_byte_quantum: int = DEFAULT_INLINE_OFFLOAD_BYTE_QUANTUM
    offload_count_quantum: int = DEFAULT_OFFLOAD_COUNT_QUANTUM

    def __post_init__(self) -> None:
        _validate_quantum("offload_byte_quantum", self.offload_byte_quantum, self.max_bytes)
        _validate_quantum(
            "inline_offload_byte_quantum",
            self.inline_offload_byte_quantum,
            self.max_inline_bytes,
        )
        _validate_quantum("offload_count_quantum", self.offload_count_quantum, self.max_images)


def _validate_quantum(name: str, quantum: int, limit: int) -> None:
    if quantum < 1:
        raise ValueError(f"{name} must be >= 1, got {quantum}")
    if quantum > limit:
        raise ValueError(f"{name} ({quantum}) must not exceed its limit ({limit})")


def base64_length(n: int) -> int:
    """Base64 length of *n* raw bytes, including padding (DSH :647-649)."""
    if not isinstance(n, int) or isinstance(n, bool):
        raise TypeError(f"byte length must be an int, got {n!r}")
    if n < 0:
        raise ValueError(f"byte length must be >= 0, got {n}")
    return 4 * ((n + 2) // 3)


def offloaded_image_prefix_count(lengths: Sequence[int], budget: ImageBudget) -> int:
    """How many oldest occurrences the route must drop to fit its raw budget.

    Exact port of DSH `offloadedImagePrefixCount` (:692-709).  `lengths` are
    represented byte lengths, oldest first.  Only the files route limits are
    consulted (`max_images`, `max_bytes`, `offload_byte_quantum`); the inline
    route is applied by :func:`required_image_offload` because its lengths are
    base64 lengths.

    The removal count follows the deterministic quanta: an excess of one image
    removes `ceil(1 / offload_count_quantum) * offload_count_quantum` images and
    an excess of one byte removes a whole byte quantum.  The resulting count is
    capped at the number of occurrences.  It then walks oldest first until both
    constraints are met; the byte constraint uses `>=` when the quantum is one
    (a quantum of one is exact accounting) and `>` otherwise, matching DSH's
    inclusive-only-at-quantum-one rule so the count is stable across providers.
    """
    values = _lengths(lengths)
    return _prefix_count(
        values,
        max_images=budget.max_images,
        max_bytes=budget.max_bytes,
        count_quantum=budget.offload_count_quantum,
        byte_quantum=budget.offload_byte_quantum,
    )


def required_image_offload(
    messages: Sequence[Message],
    budget: ImageBudget,
    *,
    version_bytes: Callable[[ImagePart], int] | None = None,
) -> int:
    """How many more oldest images a request must offload to fit *budget*.

    Images are visited in message order (the surface order used for the
    request), skipping occurrences that are already projected out.  An
    occurrence's length is either

    * `version_bytes(part)` -- the caller's exact represented request length in
      bytes (DSH's `versionBytes` callback; DSH returns raw bytes there, while
      this port asks for the already-represented length so the caller chooses
      the route), or
    * for a bare reference dict, `base64_length(ref["bytes"])` -- the inline
      representation P1 sends.

    Zero means the request fits.  A positive result is a request to append a new
    `image/offload` record for the returned oldest prefix and rebuild the
    request; it is not permission to drop images silently.

    The stricter of the raw/files route and the inline route is returned, so a
    caller that configures either cap sees it enforced.  The pure DSH algorithm
    is otherwise untouched.
    """
    lengths: list[int] = []
    for message in messages:
        for part in message.content:
            if _part_kind(part) != "image" or _is_offloaded(part):
                continue
            if version_bytes is not None:
                length = version_bytes(part)  # type: ignore[arg-type]
                if not isinstance(length, int) or isinstance(length, bool) or length < 0:
                    raise ValueError(f"version_bytes must return a non-negative int, got {length!r}")
                lengths.append(length)
            else:
                raw_bytes = _declared_bytes(part)
                if raw_bytes is None:
                    raise ValueError(
                        "image part carries no reference bytes; pass version_bytes for bare attachment ids"
                    )
                lengths.append(base64_length(raw_bytes))

    inline = _prefix_count(
        lengths,
        max_images=budget.max_images,
        max_bytes=budget.max_inline_bytes,
        count_quantum=budget.offload_count_quantum,
        byte_quantum=budget.inline_offload_byte_quantum,
    )
    files = _prefix_count(
        lengths,
        max_images=budget.max_images,
        max_bytes=budget.max_bytes,
        count_quantum=budget.offload_count_quantum,
        byte_quantum=budget.offload_byte_quantum,
    )
    return max(inline, files)


def _prefix_count(
    lengths: list[int],
    *,
    max_images: int,
    max_bytes: int,
    count_quantum: int,
    byte_quantum: int,
) -> int:
    """DSH :692-709 verbatim for one route's limits."""
    if not lengths:
        return 0
    total = sum(lengths)
    excess_count = max(0, len(lengths) - max_images)
    excess_bytes = max(0, total - max_bytes)
    if excess_count == 0 and excess_bytes == 0:
        return 0
    remove_count = 0
    if excess_count:
        remove_count = min(len(lengths), _round_up(excess_count, count_quantum))
    remove_bytes = 0
    if excess_bytes:
        remove_bytes = _round_up(excess_bytes, byte_quantum)

    count = 0
    removed_bytes = 0
    for image_bytes in lengths:
        count_done = count >= remove_count
        if byte_quantum == 1:
            bytes_done = removed_bytes >= remove_bytes
        else:
            bytes_done = removed_bytes > remove_bytes
        if count_done and (remove_bytes == 0 or bytes_done):
            break
        removed_bytes += image_bytes
        count += 1
    return count


def _round_up(value: int, quantum: int) -> int:
    return ((value + quantum - 1) // quantum) * quantum


def _lengths(lengths: Sequence[int]) -> list[int]:
    values: list[int] = []
    for value in lengths:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"image lengths must be non-negative ints, got {value!r}")
        values.append(value)
    return values


def _part_kind(part: Any) -> str:
    if isinstance(part, dict):
        value = part.get("type", "")
    else:
        value = getattr(part, "type", "")
    return value if isinstance(value, str) else ""


def _is_offloaded(part: Any) -> bool:
    """Whether the occurrence is already projected out (DSH's `offloaded: true`)."""
    if isinstance(part, dict):
        return part.get("offloaded") is True
    extra = getattr(part, "model_extra", None) or {}
    return extra.get("offloaded") is True


def _declared_bytes(part: Any) -> int | None:
    """Raw image byte count declared by an attachment reference, if present."""
    attachment = part.get("attachment") if isinstance(part, dict) else getattr(part, "attachment", None)
    if isinstance(attachment, dict):
        value = attachment.get("bytes")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


__all__ = [
    "DEFAULT_INLINE_OFFLOAD_BYTE_QUANTUM",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_IMAGES",
    "DEFAULT_MAX_INLINE_BYTES",
    "DEFAULT_OFFLOAD_BYTE_QUANTUM",
    "DEFAULT_OFFLOAD_COUNT_QUANTUM",
    "ImageBudget",
    "base64_length",
    "offloaded_image_prefix_count",
    "required_image_offload",
]
