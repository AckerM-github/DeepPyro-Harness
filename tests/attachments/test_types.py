"""Attachment reference types: DSH-wire serialization and validation."""

from __future__ import annotations

import pytest

from deeppyro.attachments import (
    ATTACHMENT_ID_PREFIX,
    IMAGE_MEDIA_TYPES,
    FileAttachmentRef,
    ImageAttachmentRef,
)


def _image_ref() -> ImageAttachmentRef:
    return ImageAttachmentRef(
        attachmentId="att-0123456789abcdef",
        mediaType="image/png",
        bytes=12345,
        width=640,
        height=480,
    )


def test_image_ref_to_dict_omits_optional_fields() -> None:
    assert _image_ref().to_dict() == {
        "attachmentId": "att-0123456789abcdef",
        "mediaType": "image/png",
        "bytes": 12345,
        "width": 640,
        "height": 480,
    }


def test_image_ref_roundtrip() -> None:
    assert ImageAttachmentRef.from_dict(_image_ref().to_dict()) == _image_ref()


def test_image_ref_roundtrip_with_optional_fields() -> None:
    ref = ImageAttachmentRef(
        attachmentId="att-0123456789abcdef",
        mediaType="image/jpeg",
        bytes=999,
        width=60,
        height=30,
        name="photo.jpg",
        originalDimensions={"width": 30, "height": 60},
    )
    data = ref.to_dict()
    assert data["name"] == "photo.jpg"
    assert data["originalDimensions"] == {"width": 30, "height": 60}
    assert ImageAttachmentRef.from_dict(data) == ref


def test_image_ref_copies_original_dimensions() -> None:
    dimensions = {"width": 30, "height": 60}
    ref = ImageAttachmentRef(
        attachmentId="att-0123456789abcdef",
        mediaType="image/png",
        bytes=1,
        width=2,
        height=3,
        originalDimensions=dimensions,
    )
    serialized = ref.to_dict()
    serialized["originalDimensions"]["width"] = 1
    assert ref.originalDimensions == dimensions


def test_attachment_id_prefix_is_att_dash() -> None:
    assert ATTACHMENT_ID_PREFIX == "att-"
    assert _image_ref().attachmentId.startswith(ATTACHMENT_ID_PREFIX)


def test_image_media_types_are_the_dsh_set() -> None:
    assert IMAGE_MEDIA_TYPES == ("image/png", "image/jpeg", "image/webp", "image/gif")


@pytest.mark.parametrize("media_type", ["image/tiff", "image/bmp", "", "png"])
def test_from_dict_rejects_unknown_media_type(media_type: str) -> None:
    data = _image_ref().to_dict()
    data["mediaType"] = media_type
    with pytest.raises(ValueError, match="media type"):
        ImageAttachmentRef.from_dict(data)


@pytest.mark.parametrize("field", ["bytes", "width", "height"])
def test_from_dict_rejects_missing_numeric_field(field: str) -> None:
    data = _image_ref().to_dict()
    del data[field]
    with pytest.raises(TypeError):
        ImageAttachmentRef.from_dict(data)


def test_from_dict_rejects_missing_id() -> None:
    data = _image_ref().to_dict()
    del data["attachmentId"]
    with pytest.raises(ValueError, match="attachmentId"):
        ImageAttachmentRef.from_dict(data)


def test_from_dict_rejects_bad_dimensions() -> None:
    data = _image_ref().to_dict()
    data["originalDimensions"] = {"width": 1}
    with pytest.raises(TypeError, match="originalDimensions"):
        ImageAttachmentRef.from_dict(data)
    data["originalDimensions"] = ["1", "2"]
    with pytest.raises(TypeError, match="originalDimensions"):
        ImageAttachmentRef.from_dict(data)


def test_from_dict_rejects_boolean_size() -> None:
    data = _image_ref().to_dict()
    data["bytes"] = True
    with pytest.raises(TypeError, match="integer"):
        ImageAttachmentRef.from_dict(data)


def test_from_dict_rejects_non_mapping() -> None:
    with pytest.raises(TypeError):
        ImageAttachmentRef.from_dict(["not", "a", "mapping"])
    with pytest.raises(TypeError):
        FileAttachmentRef.from_dict("not a mapping")


def test_direct_construction_validates_invariants() -> None:
    with pytest.raises(ValueError, match="media type"):
        ImageAttachmentRef(attachmentId="att-1", mediaType="image/tiff", bytes=1, width=1, height=1)
    with pytest.raises(ValueError, match="dimensions"):
        ImageAttachmentRef(attachmentId="att-1", mediaType="image/png", bytes=1, width=0, height=1)


def test_file_ref_roundtrip() -> None:
    ref = FileAttachmentRef(attachmentId="att-fedcba9876543210", name="notes.txt", bytes=42)
    assert ref.to_dict() == {"attachmentId": "att-fedcba9876543210", "name": "notes.txt", "bytes": 42}
    assert FileAttachmentRef.from_dict(ref.to_dict()) == ref


def test_file_ref_rejects_missing_name() -> None:
    data = {"attachmentId": "att-fedcba9876543210", "bytes": 42}
    with pytest.raises(ValueError, match="name"):
        FileAttachmentRef.from_dict(data)
