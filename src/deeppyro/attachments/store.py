"""Attachment store: validate, normalize and persist image attachments.

The store is deliberately independent of the kernel. It can be constructed
with an explicit directory and used directly from tests or SDK code; the
builtin attachments plugin wraps it and exposes it as the attachments service.

Layout::

    <directory>/
        manifest.jsonl
        att-0123456789abcdef.png

Every accepted image is decoded with Pillow, rotated according to its EXIF
orientation and re-encoded as a PNG, so a reference always points at a
provider-independent 8-bit sRGB or sRGBA raster. The manifest is append-only
JSON Lines; each record carries the sha256 digest of the stored PNG so reads
can detect corruption.

Only PNG, JPEG, WebP and GIF uploads are accepted. Every other format, and
every image that exceeds the configured pixel, dimension or byte limits, is
rejected with a stable AttachmentError code. Original upload bytes are never
embedded in durable session events: only ImageAttachmentRef values are.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from deeppyro.attachments.types import (
    ATTACHMENT_ID_PREFIX,
    IMAGE_MEDIA_TYPES,
    ImageAttachmentRef,
    ImageMediaType,
)
from deeppyro.core.errors import ConfigError
from deeppyro.paths import deeppyro_home

__all__ = [
    "ATTACHMENT_CORRUPT",
    "ATTACHMENT_NOT_FOUND",
    "DEFAULT_ATTACHMENTS_DIRNAME",
    "DEFAULT_MAX_DIMENSION",
    "DEFAULT_MAX_IMAGES_PER_MESSAGE",
    "DEFAULT_MAX_IMAGE_BYTES",
    "DEFAULT_MAX_PIXELS",
    "ERROR_CODES",
    "IMAGES_TOO_LARGE",
    "IMAGE_DIMENSION_TOO_LARGE",
    "IMAGE_TOO_MANY_PIXELS",
    "INVALID_IMAGE",
    "TOO_MANY_IMAGES",
    "UNSUPPORTED_IMAGE_TYPE",
    "AttachmentConfig",
    "AttachmentError",
    "AttachmentStore",
    "default_attachments_dir",
]

# Stable machine-readable error codes. Callers must branch on these codes and
# never parse the human-readable message.
INVALID_IMAGE = "INVALID_IMAGE"
UNSUPPORTED_IMAGE_TYPE = "UNSUPPORTED_IMAGE_TYPE"
IMAGE_TOO_MANY_PIXELS = "IMAGE_TOO_MANY_PIXELS"
IMAGE_DIMENSION_TOO_LARGE = "IMAGE_DIMENSION_TOO_LARGE"
TOO_MANY_IMAGES = "TOO_MANY_IMAGES"
IMAGES_TOO_LARGE = "IMAGES_TOO_LARGE"
ATTACHMENT_NOT_FOUND = "ATTACHMENT_NOT_FOUND"
ATTACHMENT_CORRUPT = "ATTACHMENT_CORRUPT"

ERROR_CODES: tuple[str, ...] = (
    INVALID_IMAGE,
    UNSUPPORTED_IMAGE_TYPE,
    IMAGE_TOO_MANY_PIXELS,
    IMAGE_DIMENSION_TOO_LARGE,
    TOO_MANY_IMAGES,
    IMAGES_TOO_LARGE,
    ATTACHMENT_NOT_FOUND,
    ATTACHMENT_CORRUPT,
)

DEFAULT_ATTACHMENTS_DIRNAME = "attachments"
DEFAULT_MAX_IMAGES_PER_MESSAGE = 20
DEFAULT_MAX_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PIXELS = 40_000_000
DEFAULT_MAX_DIMENSION = 16_384

_MANIFEST_NAME = "manifest.jsonl"
_ATTACHMENT_ID_RE = re.compile(r"^att-[0-9a-f]{16}$")

# Pillow format name -> DSH image media type (all normalized to PNG on disk).
_PIL_FORMATS: dict[str, ImageMediaType] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}


class AttachmentError(Exception):
    """Base error for attachment validation, persistence and reads.

    Every instance carries a stable machine-readable code; callers should
    branch on the code rather than parse the message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class AttachmentConfig(BaseModel):
    """Validated [attachments] configuration table.

    The TOML table uses camelCase keys; the model also accepts the snake_case
    field names so Python callers can pass either form.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    directory: str = ""
    max_images_per_message: int = Field(
        default=DEFAULT_MAX_IMAGES_PER_MESSAGE,
        alias="maxImagesPerMessage",
        ge=1,
        le=1000,
    )
    max_image_bytes: int = Field(default=DEFAULT_MAX_IMAGE_BYTES, alias="maxImageBytes", ge=1)
    max_pixels: int = Field(default=DEFAULT_MAX_PIXELS, alias="maxPixels", ge=1)
    max_dimension: int = Field(default=DEFAULT_MAX_DIMENSION, alias="maxDimension", ge=1)

    @classmethod
    def from_config(cls, value: Any = None) -> AttachmentConfig:
        """Coerce None, a mapping or an AttachmentConfig into validated options."""
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, BaseModel):
            value = value.model_dump()
        if isinstance(value, Mapping):
            try:
                return cls.model_validate(dict(value))
            except ValidationError as exc:
                raise ConfigError(f"invalid [attachments] configuration: {exc}") from exc
        raise ConfigError(f"[attachments] configuration must be a table, got {type(value).__name__}")


def default_attachments_dir(home: str | Path | None = None) -> Path:
    """Return the default attachments directory under the Deeppyro home."""
    root = Path(home).expanduser() if home is not None else deeppyro_home()
    return root / DEFAULT_ATTACHMENTS_DIRNAME


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AttachmentStore:
    """Filesystem-backed image attachment store.

    The store is a plain class, not a kernel service: callers construct it
    directly or receive it through the attachments service. Writes are atomic
    and the manifest is append-only, so a crash can at worst leave an orphan
    PNG without a manifest record, never a corrupt reference.
    """

    provide: ClassVar[str] = "attachments"
    inject: ClassVar[list[str]] = []

    def __init__(
        self,
        config: AttachmentConfig | Mapping[str, Any] | None = None,
        directory: str | Path | None = None,
        home: str | Path | None = None,
    ) -> None:
        self.config = AttachmentConfig.from_config(config)
        self.directory = self._resolve_directory(directory, home)
        self.manifest_path = self.directory / _MANIFEST_NAME

    # -- public API ---------------------------------------------------------

    async def save_image(
        self,
        data: bytes,
        *,
        name: str | None = None,
        media_type: str | None = None,
    ) -> ImageAttachmentRef:
        """Validate, normalize and persist one image, returning its reference.

        The stored payload is always a PNG. Raises AttachmentError with a
        stable code for empty/corrupt bytes, unsupported formats, oversized
        images or oversized payloads.
        """
        payload = self._coerce_bytes(data)
        self._check_payload_size(payload)
        declared = self._declared_media_type(media_type)
        pil_format, source_width, source_height = self._inspect_header(payload)
        detected = _PIL_FORMATS[pil_format]
        if declared is not None and declared != detected:
            raise AttachmentError(
                INVALID_IMAGE,
                f"declared media type {declared!r} does not match decoded format {pil_format!r}",
            )
        self._check_header_limits(source_width, source_height)
        png_bytes, width, height = self._encode_png(payload)
        if len(png_bytes) > self.config.max_image_bytes:
            raise AttachmentError(
                IMAGES_TOO_LARGE,
                f"normalized image is {len(png_bytes)} bytes, limit is {self.config.max_image_bytes}",
            )
        attachment_id = ATTACHMENT_ID_PREFIX + uuid4().hex[:16]
        digest = hashlib.sha256(png_bytes).hexdigest()
        target = self._path_for_id(attachment_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(target, png_bytes)
        entry = {
            "id": attachment_id,
            "mediaType": "image/png",
            "bytes": len(png_bytes),
            "width": width,
            "height": height,
            "name": name,
            "digest": digest,
            "created_at": _utc_now(),
        }
        self._append_manifest(entry)
        original_dimensions = None
        if (width, height) != (source_width, source_height):
            original_dimensions = {"width": source_width, "height": source_height}
        return ImageAttachmentRef(
            attachmentId=attachment_id,
            mediaType="image/png",
            bytes=len(png_bytes),
            width=width,
            height=height,
            name=name,
            originalDimensions=original_dimensions,
        )

    def image_host_path(self, ref: ImageAttachmentRef) -> Path:
        """Return the normalized read-only path for a reference."""
        return self._path_for_id(ref.attachmentId)

    def read_image_bytes(self, ref: ImageAttachmentRef) -> bytes:
        """Read a stored image after verifying its bytes, digest and dimensions."""
        path = self.image_host_path(ref)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise AttachmentError(
                ATTACHMENT_NOT_FOUND,
                f"attachment {ref.attachmentId!r} does not exist",
            ) from exc
        except OSError as exc:
            raise AttachmentError(
                ATTACHMENT_CORRUPT,
                f"cannot read attachment {ref.attachmentId!r}: {exc}",
            ) from exc
        if len(data) != ref.bytes:
            raise AttachmentError(
                ATTACHMENT_CORRUPT,
                f"attachment {ref.attachmentId!r} has {len(data)} bytes, expected {ref.bytes}",
            )
        entry = self._find_entry(ref.attachmentId)
        if entry is None:
            raise AttachmentError(
                ATTACHMENT_CORRUPT,
                f"attachment {ref.attachmentId!r} has no manifest record",
            )
        expected_digest = entry.get("digest")
        actual_digest = hashlib.sha256(data).hexdigest()
        if not isinstance(expected_digest, str) or actual_digest != expected_digest:
            raise AttachmentError(
                ATTACHMENT_CORRUPT,
                f"attachment {ref.attachmentId!r} digest mismatch",
            )
        self._verify_png(data, ref)
        return data

    def lookup(self, attachment_id: str) -> ImageAttachmentRef | None:
        """Resolve an attachment id from the manifest, or return None.

        Corrupt manifest lines and records pointing at missing files are
        skipped instead of raising: lookup is a best-effort recovery path.
        """
        if not isinstance(attachment_id, str) or not _ATTACHMENT_ID_RE.match(attachment_id):
            return None
        entry = self._find_entry(attachment_id)
        if entry is None:
            return None
        try:
            ref = ImageAttachmentRef.from_dict(
                {
                    "attachmentId": entry.get("id"),
                    "mediaType": entry.get("mediaType", "image/png"),
                    "bytes": entry.get("bytes"),
                    "width": entry.get("width"),
                    "height": entry.get("height"),
                    "name": entry.get("name"),
                }
            )
        except (TypeError, ValueError):
            return None
        if not self._path_for_id(attachment_id).is_file():
            return None
        return ref

    def validate_image_batch(self, refs: Iterable[ImageAttachmentRef]) -> None:
        """Check per-message image count and total byte budget."""
        items = list(refs)
        if len(items) > self.config.max_images_per_message:
            raise AttachmentError(
                TOO_MANY_IMAGES,
                f"message has {len(items)} images, limit is {self.config.max_images_per_message}",
            )
        total = sum(ref.bytes for ref in items)
        if total > self.config.max_image_bytes:
            raise AttachmentError(
                IMAGES_TOO_LARGE,
                f"message images total {total} bytes, limit is {self.config.max_image_bytes}",
            )

    # -- configuration ------------------------------------------------------

    def _resolve_directory(self, directory: str | Path | None, home: str | Path | None) -> Path:
        if directory is not None:
            root = Path(directory).expanduser()
        elif self.config.directory:
            root = Path(self.config.directory).expanduser()
        else:
            return default_attachments_dir(home)
        if not root.is_absolute():
            root = root.resolve()
        return root

    # -- validation helpers -------------------------------------------------

    @staticmethod
    def _coerce_bytes(data: bytes) -> bytes:
        if isinstance(data, (bytes, bytearray, memoryview)):
            return bytes(data)
        raise AttachmentError(INVALID_IMAGE, f"image data must be bytes, got {type(data).__name__}")

    def _check_payload_size(self, payload: bytes) -> None:
        if not payload:
            raise AttachmentError(INVALID_IMAGE, "image data is empty")
        if len(payload) > self.config.max_image_bytes:
            raise AttachmentError(
                IMAGES_TOO_LARGE,
                f"image is {len(payload)} bytes, limit is {self.config.max_image_bytes}",
            )

    @staticmethod
    def _declared_media_type(media_type: str | None) -> ImageMediaType | None:
        if media_type is None:
            return None
        normalized = media_type.strip().lower()
        if normalized == "image/jpg":
            normalized = "image/jpeg"
        if normalized not in IMAGE_MEDIA_TYPES:
            raise AttachmentError(
                UNSUPPORTED_IMAGE_TYPE,
                f"unsupported image media type {media_type!r}",
            )
        return cast(ImageMediaType, normalized)

    @staticmethod
    def _inspect_header(payload: bytes) -> tuple[str, int, int]:
        try:
            with Image.open(BytesIO(payload)) as image:
                pil_format = (image.format or "").upper()
                width, height = image.size
        except Image.DecompressionBombError as exc:
            raise AttachmentError(
                IMAGE_TOO_MANY_PIXELS,
                "image exceeds the Pillow decompression bomb limit",
            ) from exc
        except UnidentifiedImageError as exc:
            raise AttachmentError(INVALID_IMAGE, "bytes are not a recognizable image") from exc
        except (OSError, ValueError) as exc:
            raise AttachmentError(INVALID_IMAGE, f"cannot read image header: {exc}") from exc
        if pil_format not in _PIL_FORMATS:
            supported = ", ".join(sorted(_PIL_FORMATS))
            raise AttachmentError(
                UNSUPPORTED_IMAGE_TYPE,
                f"unsupported image format {pil_format or 'unknown'!r}; supported formats are {supported}",
            )
        return pil_format, width, height

    def _check_header_limits(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise AttachmentError(INVALID_IMAGE, f"invalid image dimensions {width}x{height}")
        if width > self.config.max_dimension or height > self.config.max_dimension:
            raise AttachmentError(
                IMAGE_DIMENSION_TOO_LARGE,
                f"image edge {max(width, height)}px exceeds the {self.config.max_dimension}px limit",
            )
        if width * height > self.config.max_pixels:
            raise AttachmentError(
                IMAGE_TOO_MANY_PIXELS,
                f"image has {width * height} pixels, limit is {self.config.max_pixels}",
            )

    # -- image normalization ------------------------------------------------

    @staticmethod
    def _encode_png(payload: bytes) -> tuple[bytes, int, int]:
        try:
            with Image.open(BytesIO(payload)) as image:
                image.load()
                transposed = ImageOps.exif_transpose(image)
                try:
                    raster = AttachmentStore._to_srgb_raster(transposed)
                    try:
                        buffer = BytesIO()
                        raster.save(buffer, format="PNG")
                        return buffer.getvalue(), raster.width, raster.height
                    finally:
                        raster.close()
                finally:
                    if transposed is not image:
                        transposed.close()
        except AttachmentError:
            raise
        except Image.DecompressionBombError as exc:
            raise AttachmentError(
                IMAGE_TOO_MANY_PIXELS,
                "image exceeds the Pillow decompression bomb limit",
            ) from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AttachmentError(INVALID_IMAGE, f"cannot decode image: {exc}") from exc

    @staticmethod
    def _to_srgb_raster(image: Image.Image) -> Image.Image:
        has_alpha = image.mode in {"RGBA", "LA", "PA"} or (image.mode == "P" and "transparency" in image.info)
        try:
            return image.convert("RGBA" if has_alpha else "RGB")
        except (OSError, ValueError) as exc:
            raise AttachmentError(
                INVALID_IMAGE,
                f"cannot normalize image mode {image.mode!r}: {exc}",
            ) from exc

    # -- filesystem helpers -------------------------------------------------

    def _path_for_id(self, attachment_id: str) -> Path:
        if not isinstance(attachment_id, str) or not _ATTACHMENT_ID_RE.match(attachment_id):
            raise AttachmentError(
                ATTACHMENT_NOT_FOUND,
                f"invalid attachment id: {attachment_id!r}",
            )
        return self.directory / f"{attachment_id}.png"

    @staticmethod
    def _write_atomic(target: Path, data: bytes) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise

    def _append_manifest(self, entry: Mapping[str, Any]) -> None:
        line = json.dumps(dict(entry), ensure_ascii=False, sort_keys=True)
        with self.manifest_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _iter_manifest(self) -> Iterator[dict[str, Any]]:
        try:
            text = self.manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeDecodeError) as exc:
            raise AttachmentError(
                ATTACHMENT_CORRUPT,
                f"cannot read attachment manifest: {exc}",
            ) from exc
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry

    def _find_entry(self, attachment_id: str) -> dict[str, Any] | None:
        found: dict[str, Any] | None = None
        for entry in self._iter_manifest():
            if entry.get("id") == attachment_id:
                found = entry
        return found

    @staticmethod
    def _verify_png(data: bytes, ref: ImageAttachmentRef) -> None:
        try:
            with Image.open(BytesIO(data)) as image:
                if (image.format or "").upper() != "PNG":
                    raise AttachmentError(
                        ATTACHMENT_CORRUPT,
                        f"attachment {ref.attachmentId!r} is not a PNG",
                    )
                if image.size != (ref.width, ref.height):
                    raise AttachmentError(
                        ATTACHMENT_CORRUPT,
                        f"attachment {ref.attachmentId!r} is {image.width}x{image.height}, "
                        f"expected {ref.width}x{ref.height}",
                    )
        except AttachmentError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise AttachmentError(
                ATTACHMENT_CORRUPT,
                f"attachment {ref.attachmentId!r} is not a readable PNG",
            ) from exc
