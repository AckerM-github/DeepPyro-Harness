"""Image attachment storage for the DeepPyro Harness.

Public API: the DSH-compatible reference types and the AttachmentStore that
validates, normalizes and persists image bytes. This package deliberately has
no kernel imports, so SDK code and tests can construct a store directly. The
builtin attachments plugin (deeppyro.builtin.attachments) only resolves
configuration and registers the store as a service.
"""

from __future__ import annotations

from deeppyro.attachments.store import (
    ATTACHMENT_CORRUPT,
    ATTACHMENT_NOT_FOUND,
    IMAGE_DIMENSION_TOO_LARGE,
    IMAGE_TOO_MANY_PIXELS,
    IMAGES_TOO_LARGE,
    INVALID_IMAGE,
    TOO_MANY_IMAGES,
    UNSUPPORTED_IMAGE_TYPE,
    AttachmentConfig,
    AttachmentError,
    AttachmentStore,
    default_attachments_dir,
)
from deeppyro.attachments.types import (
    ATTACHMENT_ID_PREFIX,
    IMAGE_MEDIA_TYPES,
    FileAttachmentRef,
    ImageAttachmentRef,
    ImageMediaType,
)

__all__ = [
    "ATTACHMENT_CORRUPT",
    "ATTACHMENT_ID_PREFIX",
    "ATTACHMENT_NOT_FOUND",
    "IMAGES_TOO_LARGE",
    "IMAGE_DIMENSION_TOO_LARGE",
    "IMAGE_MEDIA_TYPES",
    "IMAGE_TOO_MANY_PIXELS",
    "INVALID_IMAGE",
    "TOO_MANY_IMAGES",
    "UNSUPPORTED_IMAGE_TYPE",
    "AttachmentConfig",
    "AttachmentError",
    "AttachmentStore",
    "FileAttachmentRef",
    "ImageAttachmentRef",
    "ImageMediaType",
    "default_attachments_dir",
]
