"""Image budget tests: DSH offload prefix algorithm and resolver-driven offload counts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from deeppyro.core.wire import ImagePart, Message, TextPart
from deeppyro.llm.image_budget import (
    DEFAULT_INLINE_OFFLOAD_BYTE_QUANTUM,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_IMAGES,
    DEFAULT_MAX_INLINE_BYTES,
    DEFAULT_OFFLOAD_BYTE_QUANTUM,
    DEFAULT_OFFLOAD_COUNT_QUANTUM,
    ImageBudget,
    base64_length,
    offloaded_image_prefix_count,
    required_image_offload,
)


def _budget(**overrides: int) -> ImageBudget:
    """A permissive budget whose only active limits are the overridden ones."""
    values: dict[str, int] = {
        "max_images": 1_000,
        "max_bytes": 10**9,
        "max_inline_bytes": 10**9,
        "offload_byte_quantum": 1,
        "inline_offload_byte_quantum": 1,
        "offload_count_quantum": 1,
    }
    values.update(overrides)
    return ImageBudget(**values)


def _ref(size: int, *, media_type: str = "image/png", name: str = "a.png") -> dict[str, object]:
    return {
        "attachmentId": "att-0000000000000001",
        "mediaType": media_type,
        "bytes": size,
        "width": 1,
        "height": 1,
        "name": name,
    }


def _image_message(*sizes: int) -> Message:
    return Message(
        role="user",
        content=[TextPart(text="look")] + [ImagePart(attachment=_ref(size)) for size in sizes],
    )


# ---------------------------------------------------------------------------
# ImageBudget
# ---------------------------------------------------------------------------


def test_default_budget_matches_dsh_constants() -> None:
    budget = ImageBudget()
    assert budget.max_images == DEFAULT_MAX_IMAGES == 600
    assert budget.max_bytes == DEFAULT_MAX_BYTES == 134_217_728
    assert budget.max_inline_bytes == DEFAULT_MAX_INLINE_BYTES == 20_971_520
    assert budget.offload_byte_quantum == DEFAULT_OFFLOAD_BYTE_QUANTUM == 67_108_864
    assert budget.inline_offload_byte_quantum == DEFAULT_INLINE_OFFLOAD_BYTE_QUANTUM == 10_485_760
    assert budget.offload_count_quantum == DEFAULT_OFFLOAD_COUNT_QUANTUM == 20


def test_budget_is_frozen() -> None:
    budget = ImageBudget()
    with pytest.raises(FrozenInstanceError):
        budget.max_images = 1  # type: ignore[misc]


@pytest.mark.parametrize("quantum", [0, -1])
def test_budget_rejects_non_positive_quanta(quantum: int) -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        ImageBudget(offload_byte_quantum=quantum)
    with pytest.raises(ValueError, match="must be >= 1"):
        ImageBudget(inline_offload_byte_quantum=quantum)
    with pytest.raises(ValueError, match="must be >= 1"):
        ImageBudget(offload_count_quantum=quantum)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_bytes": 10, "offload_byte_quantum": 11}, "offload_byte_quantum"),
        ({"max_inline_bytes": 10, "inline_offload_byte_quantum": 11}, "inline_offload_byte_quantum"),
        ({"max_images": 3, "offload_count_quantum": 4}, "offload_count_quantum"),
    ],
)
def test_budget_rejects_quantum_above_limit(kwargs: dict[str, int], match: str) -> None:
    with pytest.raises(ValueError, match="must not exceed") as info:
        ImageBudget(**kwargs)
    assert match in str(info.value)


# ---------------------------------------------------------------------------
# base64_length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [(0, 0), (1, 4), (2, 4), (3, 4), (4, 8), (5, 8), (6, 8), (7, 12), (100, 136)],
)
def test_base64_length(raw: int, encoded: int) -> None:
    assert base64_length(raw) == encoded


@pytest.mark.parametrize("value", [True, 1.5, "3"])
def test_base64_length_rejects_non_ints(value: object) -> None:
    with pytest.raises(TypeError):
        base64_length(value)  # type: ignore[arg-type]


def test_base64_length_rejects_negative() -> None:
    with pytest.raises(ValueError):
        base64_length(-1)


# ---------------------------------------------------------------------------
# offloaded_image_prefix_count
# ---------------------------------------------------------------------------


def test_prefix_count_empty_and_within_budget() -> None:
    budget = _budget()
    assert offloaded_image_prefix_count([], budget) == 0
    assert offloaded_image_prefix_count([4, 4], budget) == 0
    assert offloaded_image_prefix_count([4, 4], _budget(max_images=2, max_bytes=8)) == 0


def test_prefix_count_count_excess_uses_count_quantum() -> None:
    budget = _budget(max_images=2, offload_count_quantum=1)
    assert offloaded_image_prefix_count([5, 5, 5, 5], budget) == 2


def test_prefix_count_caps_rounded_count_at_length() -> None:
    budget = _budget(max_images=20, offload_count_quantum=20)
    # 30 occurrences exceed by 10; the quantum rounds that up to 20, never beyond len.
    assert offloaded_image_prefix_count([1] * 30, budget) == 20


def test_prefix_count_byte_excess_with_quantum_one_is_inclusive() -> None:
    # excess = 2 bytes; the first occurrence is 6 bytes: 6 >= 2 stops after it.
    budget = _budget(max_bytes=10, offload_byte_quantum=1)
    assert offloaded_image_prefix_count([6, 6], budget) == 1


def test_prefix_count_byte_excess_with_large_quantum_is_strict() -> None:
    # excess rounds up to 10; 10 > 10 is false, so both occurrences are removed.
    budget = _budget(max_bytes=10, offload_byte_quantum=10)
    assert offloaded_image_prefix_count([10, 10], budget) == 2


def test_prefix_count_removes_oldest_prefix_only() -> None:
    budget = _budget(max_bytes=2, offload_byte_quantum=1)
    assert offloaded_image_prefix_count([100, 1, 1], budget) == 1


def test_prefix_count_requires_count_and_bytes_to_be_satisfied() -> None:
    budget = _budget(max_images=2, max_bytes=10)
    # excess count 2, excess bytes 6; both are met after two occurrences.
    assert offloaded_image_prefix_count([4, 4, 4, 4], budget) == 2


def test_prefix_count_keeps_going_while_bytes_are_not_met() -> None:
    budget = _budget(max_images=2, max_bytes=1)
    # count excess is satisfied by one image, bytes need two.
    assert offloaded_image_prefix_count([1, 1, 1], budget) == 2


def test_prefix_count_rejects_negative_lengths() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        offloaded_image_prefix_count([4, -1], _budget())


# ---------------------------------------------------------------------------
# required_image_offload
# ---------------------------------------------------------------------------


def test_required_offload_no_images() -> None:
    messages = [Message(role="user", content=[TextPart(text="hi")])]
    assert required_image_offload(messages, _budget()) == 0


def test_required_offload_within_budget_is_zero() -> None:
    messages = [_image_message(3), _image_message(3)]
    assert required_image_offload(messages, _budget()) == 0


def test_required_offload_counts_oldest_images_across_messages() -> None:
    messages = [_image_message(3, 3), _image_message(3)]
    budget = _budget(max_images=2)
    assert required_image_offload(messages, budget) == 1


def test_required_offload_uses_dict_bytes_as_base64_length() -> None:
    messages = [_image_message(3), _image_message(3), _image_message(3)]
    # 3 raw bytes -> 4 base64 bytes; 12 base64 bytes exceed the 8-byte inline cap.
    budget = _budget(max_inline_bytes=8, max_bytes=10**9)
    assert required_image_offload(messages, budget) == 1


def test_required_offload_also_honours_raw_byte_cap() -> None:
    messages = [_image_message(3), _image_message(3), _image_message(3)]
    budget = _budget(max_inline_bytes=10**9, max_bytes=8)
    assert required_image_offload(messages, budget) == 1


def test_required_offload_version_bytes_is_represented_length_directly() -> None:
    messages = [_image_message(1), _image_message(1), _image_message(1)]
    budget = _budget(max_inline_bytes=2, max_bytes=2)
    assert required_image_offload(messages, budget, version_bytes=lambda part: 1) == 1


def test_required_offload_version_bytes_sees_the_original_part() -> None:
    seen: list[object] = []

    def version_bytes(part: ImagePart) -> int:
        seen.append(part)
        return 5

    messages = [_image_message(3), _image_message(3)]
    assert required_image_offload(messages, _budget(max_images=1), version_bytes=version_bytes) == 1
    assert len(seen) == 2
    assert all(isinstance(part, ImagePart) for part in seen)


def test_required_offload_skips_offloaded_parts() -> None:
    messages = [
        Message(
            role="user",
            content=[
                ImagePart(attachment=_ref(3), offloaded=True),
                ImagePart(attachment=_ref(3)),
            ],
        )
    ]
    assert required_image_offload(messages, _budget(max_images=1)) == 0


def test_required_offload_skips_offloaded_dict_parts() -> None:
    messages = [
        Message(
            role="user",
            content=[
                {"type": "image", "attachment": _ref(3), "offloaded": True},
                {"type": "image", "attachment": _ref(3)},
            ],
        )
    ]
    assert required_image_offload(messages, _budget(max_images=1)) == 0


@pytest.mark.parametrize("value", [-1, True, 1.5, "4"])
def test_required_offload_rejects_bad_version_bytes(value: object) -> None:
    with pytest.raises(ValueError, match="version_bytes"):
        required_image_offload([_image_message(3)], _budget(), version_bytes=lambda part: value)  # type: ignore[arg-type,return-value]


def test_required_offload_requires_bytes_or_version_bytes() -> None:
    message = Message(role="user", content=[ImagePart(attachment="att-bare-id")])
    with pytest.raises(ValueError, match="version_bytes"):
        required_image_offload([message], _budget())
