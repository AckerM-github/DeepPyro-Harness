"""Attachment reference types shared by the store, tools and transports.

Phase 1 of image offload stores image bytes in an AttachmentStore and keeps
only small references in durable session events. The dataclasses in this
module mirror the DeepSeek Harness (DSH) wire format exactly (camelCase keys),
so the same payloads round-trip without translation.

Image reference wire shape::

    {
        "attachmentId": "att-0123456789abcdef",
        "mediaType": "image/png",
        "bytes": 12345,
        "width": 640,
        "height": 480,
        "name": "photo.jpg",
        "originalDimensions": {"width": 1280, "height": 960}
    }

Only name and originalDimensions are optional. File references are declared
for forward compatibility; phase 1 never creates them, but they share the same
serialization rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

__all__ = [
    "ATTACHMENT_ID_PREFIX",
    "IMAGE_MEDIA_TYPES",
    "FileAttachmentRef",
    "ImageAttachmentRef",
    "ImageMediaType",
]

ImageMediaType = Literal["image/png", "image/jpeg", "image/webp", "image/gif"]

IMAGE_MEDIA_TYPES: tuple[ImageMediaType, ...] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
)

ATTACHMENT_ID_PREFIX = "att-"


def _require_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"attachment field {key!r} must be an integer, got {value!r}")
    return value


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"attachment field {key!r} must be a non-empty string, got {value!r}")
    return value


def _optional_name(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"attachment field 'name' must be a string, got {value!r}")
    return value or None


def _optional_dimensions(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"attachment originalDimensions must be a mapping, got {value!r}")
    width = value.get("width")
    height = value.get("height")
    if (
        isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
    ):
        raise TypeError(f"attachment originalDimensions must contain integer width/height, got {value!r}")
    return {"width": width, "height": height}


@dataclass(frozen=True, slots=True)
class ImageAttachmentRef:
    """A stored, normalized image attachment.

    bytes is the size of the normalized PNG payload, not the size of the
    original upload. originalDimensions preserves the source pixel size when
    normalization changed it (for example after an EXIF rotation).
    """

    attachmentId: str
    mediaType: ImageMediaType
    bytes: int
    width: int
    height: int
    name: str | None = None
    originalDimensions: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if not self.attachmentId:
            raise ValueError("attachment id must not be empty")
        if self.mediaType not in IMAGE_MEDIA_TYPES:
            raise ValueError(f"unsupported image media type: {self.mediaType!r}")
        if self.bytes < 0:
            raise ValueError(f"attachment byte size must be non-negative, got {self.bytes}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"attachment dimensions must be positive, got {self.width}x{self.height}")

    def to_dict(self) -> dict[str, Any]:
        """Return the DSH-wire mapping for this reference."""
        data: dict[str, Any] = {
            "attachmentId": self.attachmentId,
            "mediaType": self.mediaType,
            "bytes": self.bytes,
            "width": self.width,
            "height": self.height,
        }
        if self.name is not None:
            data["name"] = self.name
        if self.originalDimensions is not None:
            data["originalDimensions"] = dict(self.originalDimensions)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImageAttachmentRef:
        """Build a reference from its DSH-wire mapping.

        Raise TypeError for wrong field types and ValueError for invalid values.
        """
        if not isinstance(data, dict):
            raise TypeError(f"image attachment must be a mapping, got {type(data).__name__}")
        media_type = data.get("mediaType")
        if media_type not in IMAGE_MEDIA_TYPES:
            raise ValueError(f"unsupported image media type: {media_type!r}")
        return cls(
            attachmentId=_require_str(data, "attachmentId"),
            mediaType=cast(ImageMediaType, media_type),
            bytes=_require_int(data, "bytes"),
            width=_require_int(data, "width"),
            height=_require_int(data, "height"),
            name=_optional_name(data.get("name")),
            originalDimensions=_optional_dimensions(data.get("originalDimensions")),
        )


@dataclass(frozen=True, slots=True)
class FileAttachmentRef:
    """A stored opaque file attachment (declared for forward compatibility)."""

    attachmentId: str
    name: str
    bytes: int

    def __post_init__(self) -> None:
        if not self.attachmentId:
            raise ValueError("attachment id must not be empty")
        if not self.name:
            raise ValueError("attachment name must not be empty")
        if self.bytes < 0:
            raise ValueError(f"attachment byte size must be non-negative, got {self.bytes}")

    def to_dict(self) -> dict[str, Any]:
        """Return the DSH-wire mapping for this reference."""
        return {"attachmentId": self.attachmentId, "name": self.name, "bytes": self.bytes}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileAttachmentRef:
        """Build a reference from its DSH-wire mapping.

        Raise TypeError for wrong field types and ValueError for invalid values.
        """
        if not isinstance(data, dict):
            raise TypeError(f"file attachment must be a mapping, got {type(data).__name__}")
        return cls(
            attachmentId=_require_str(data, "attachmentId"),
            name=_require_str(data, "name"),
            bytes=_require_int(data, "bytes"),
        )
