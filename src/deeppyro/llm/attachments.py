"""Attachment/image resolution for the native message converters.

The session log never stores base64: image parts carry an attachment reference or a
ready URL.  This module turns a part into a URL an OpenAI-compatible API accepts:

* a ready `data:`/`http(s)` URL string is forwarded unchanged;
* a dict with `url`, or a dict with base64 `data` plus `mimeType`/`mediaType`;
* a bare attachment reference (string id or reference dict) resolved through an
  optional *attachments* resolver.

The resolver is duck-typed after the attachment store service (see
docs/spec/attachments.md): it only needs `read_image_bytes(ref) -> bytes` or
`image_host_path(ref) -> Path`; an optional `lookup(attachment_id)` maps a bare id
to its reference dict.  Resolved bytes become an inline data URL in memory.

The module is wire-free (standard library only, no `core.wire`/`core.protocols`), so
both native transports can share it.  *default_media_type* is the native
`ImagePart.media_type` fallback: the conversion path maps an ImagePart without a media
type unless the reference carries its own, so passing this fallback is a strict
improvement that produces the same wire bytes when the reference already carries its
own media type.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

__all__ = ["image_part_url"]


def image_part_url(
    part: Any,
    *,
    attachments: Any = None,
    default_media_type: str | None = None,
) -> str | None:
    """Resolve an image part to a URL the API accepts, or None when unresolvable.

    Supported shapes:

    * a ready data:/http(s) URL string (forwarded unchanged);
    * a dict with url, or a dict with base64 data plus mimeType/mediaType;
    * a bare attachment reference (string id or reference dict) resolved through the
      optional *attachments* resolver.

    Resolver failures degrade to None so a broken attachment can never surface as a
    provider exception.
    """
    if hasattr(part, "attachment"):
        attachment: Any = part.attachment
    elif isinstance(part, dict):
        attachment = part.get("attachment")
    else:
        return None
    if isinstance(attachment, str):
        if attachment.startswith(("data:", "http://", "https://")):
            return attachment
        if attachments is None:
            return None
        ref = _lookup_attachment(attachments, attachment)
        return _resolved_data_url(attachments, ref if ref is not None else attachment, default_media_type)
    if isinstance(attachment, dict):
        url = attachment.get("url")
        if isinstance(url, str) and url:
            return url
        data = attachment.get("data") or attachment.get("base64")
        mime = attachment.get("mimeType") or attachment.get("mediaType") or default_media_type or "image/png"
        if isinstance(data, str) and data:
            return f"data:{mime};base64,{data}"
        if attachments is not None:
            return _resolved_data_url(attachments, attachment, default_media_type)
    return None


def _lookup_attachment(attachments: Any, attachment_id: str) -> Any | None:
    """Map a bare attachment id to its reference; None when the resolver cannot."""
    lookup = getattr(attachments, "lookup", None)
    if not callable(lookup):
        return None
    try:
        return lookup(attachment_id)
    # boundary: the attachment lookup is a plugin-provided resolver
    except Exception:  # noqa: BLE001 - an unresolvable attachment is simply skipped
        return None


def _resolved_data_url(attachments: Any, ref: Any, default_media_type: str | None = None) -> str | None:
    """Read one resolved reference as bytes and encode it as an inline data URL."""
    try:
        raw = _read_attachment_bytes(attachments, ref)
    # boundary: reading attachment bytes calls the plugin attachment service and the filesystem
    except Exception:  # noqa: BLE001 - provider conversion must never fail on a bad ref
        return None
    if not raw:
        return None
    mime = _ref_media_type(ref) or default_media_type or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _read_attachment_bytes(attachments: Any, ref: Any) -> bytes | None:
    reader = getattr(attachments, "read_image_bytes", None)
    if callable(reader):
        data = reader(ref)
        if isinstance(data, (bytes, bytearray, memoryview)):
            return bytes(data)
        return None
    host_path = getattr(attachments, "image_host_path", None)
    if callable(host_path):
        path = host_path(ref)
        if not isinstance(path, (str, Path)):
            return None
        return Path(path).read_bytes()
    return None


def _ref_media_type(ref: Any) -> str | None:
    if isinstance(ref, dict):
        value = ref.get("mediaType") or ref.get("mimeType")
    else:
        value = getattr(ref, "mediaType", None)
    return value if isinstance(value, str) and value else None
