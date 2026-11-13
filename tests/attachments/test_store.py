"""AttachmentStore: validation, normalization, persistence and corruption handling."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from deeppyro.attachments import (
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
    ImageAttachmentRef,
    default_attachments_dir,
)
from deeppyro.core.errors import ConfigError


def _image_bytes(
    fmt: str = "PNG",
    size: tuple[int, int] = (10, 5),
    color: tuple[int, ...] = (255, 0, 0),
    mode: str = "RGB",
    exif_orientation: int | None = None,
) -> bytes:
    image = Image.new(mode, size, color)
    buffer = BytesIO()
    kwargs: dict[str, object] = {}
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation
        kwargs["exif"] = exif
    try:
        image.save(buffer, format=fmt, **kwargs)
    finally:
        image.close()
    return buffer.getvalue()


def _png_bytes(size: tuple[int, int] = (10, 5)) -> bytes:
    return _image_bytes("PNG", size=size)


def _store(tmp_path: Path, **config: object) -> AttachmentStore:
    return AttachmentStore(config=config or None, directory=tmp_path / "attachments")


async def test_save_png_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _png_bytes()
    ref = await store.save_image(payload, name="shot.png")
    assert ref.attachmentId.startswith("att-")
    assert ref.mediaType == "image/png"
    assert (ref.width, ref.height) == (10, 5)
    assert ref.name == "shot.png"
    path = store.image_host_path(ref)
    assert path == store.directory / f"{ref.attachmentId}.png"
    assert path.is_file()
    assert path.stat().st_size == ref.bytes
    data = store.read_image_bytes(ref)
    assert data == path.read_bytes()
    with Image.open(BytesIO(data)) as stored:
        assert stored.format == "PNG"
        assert stored.size == (10, 5)
        assert stored.mode == "RGB"


async def test_manifest_record_is_appended(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes(), name="a.png")
    lines = store.manifest_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["id"] == ref.attachmentId
    assert entry["mediaType"] == "image/png"
    assert entry["bytes"] == ref.bytes
    assert entry["width"] == 10
    assert entry["height"] == 5
    assert entry["name"] == "a.png"
    assert len(entry["digest"]) == 64
    assert entry["created_at"].endswith("Z")


@pytest.mark.parametrize("fmt", ["JPEG", "WEBP", "GIF"])
async def test_other_formats_are_normalized_to_png(tmp_path: Path, fmt: str) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_image_bytes(fmt))
    assert ref.mediaType == "image/png"
    data = store.read_image_bytes(ref)
    with Image.open(BytesIO(data)) as stored:
        assert stored.format == "PNG"
        assert stored.size == (10, 5)


async def test_exif_orientation_is_applied(tmp_path: Path) -> None:
    store = _store(tmp_path)
    payload = _image_bytes("JPEG", size=(6, 3), exif_orientation=6)
    ref = await store.save_image(payload)
    assert (ref.width, ref.height) == (3, 6)
    assert ref.originalDimensions == {"width": 6, "height": 3}
    data = store.read_image_bytes(ref)
    with Image.open(BytesIO(data)) as stored:
        assert stored.size == (3, 6)


async def test_rgba_images_keep_alpha(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_image_bytes("PNG", mode="RGBA", color=(255, 0, 0, 128)))
    data = store.read_image_bytes(ref)
    with Image.open(BytesIO(data)) as stored:
        assert stored.mode == "RGBA"


async def test_one_pixel_image(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes(size=(1, 1)))
    assert (ref.width, ref.height) == (1, 1)
    assert len(store.read_image_bytes(ref)) > 0


async def test_directory_is_created_only_on_save(tmp_path: Path) -> None:
    directory = tmp_path / "nested" / "attachments"
    store = AttachmentStore(directory=directory)
    assert not directory.exists()
    await store.save_image(_png_bytes())
    assert directory.is_dir()
    assert (directory / "manifest.jsonl").is_file()


async def test_default_directory_honors_home_argument(tmp_path: Path) -> None:
    store = AttachmentStore(home=tmp_path)
    assert store.directory == tmp_path / "attachments"
    assert default_attachments_dir(home=tmp_path) == tmp_path / "attachments"


async def test_default_directory_honors_deeppyro_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path))
    store = AttachmentStore()
    assert store.directory == tmp_path / "attachments"


async def test_lookup_returns_saved_reference(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes())
    found = store.lookup(ref.attachmentId)
    assert found is not None
    assert found.attachmentId == ref.attachmentId
    assert found.bytes == ref.bytes
    assert (found.width, found.height) == (ref.width, ref.height)
    assert store.lookup("att-0000000000000000") is None
    assert store.lookup("not-an-id") is None
    assert store.lookup("") is None


async def test_lookup_skips_records_with_missing_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes())
    store.image_host_path(ref).unlink()
    assert store.lookup(ref.attachmentId) is None


async def test_manifest_corruption_is_tolerated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes(), name="good.png")
    with store.manifest_path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n\n")
    assert store.lookup(ref.attachmentId) is not None
    assert store.read_image_bytes(ref)
    second = await store.save_image(_png_bytes(size=(3, 2)))
    assert store.lookup(second.attachmentId) is not None


async def test_empty_and_corrupt_payloads_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AttachmentError) as empty:
        await store.save_image(b"")
    assert empty.value.code == INVALID_IMAGE
    with pytest.raises(AttachmentError) as corrupt:
        await store.save_image(b"definitely not an image")
    assert corrupt.value.code == INVALID_IMAGE


async def test_non_bytes_payload_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AttachmentError) as info:
        await store.save_image("text")  # type: ignore[arg-type]
    assert info.value.code == INVALID_IMAGE


async def test_unsupported_formats_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AttachmentError) as info:
        await store.save_image(_image_bytes("BMP"))
    assert info.value.code == UNSUPPORTED_IMAGE_TYPE


async def test_declared_media_type_mismatch_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(AttachmentError) as info:
        await store.save_image(_png_bytes(), media_type="image/jpeg")
    assert info.value.code == INVALID_IMAGE
    with pytest.raises(AttachmentError) as unsupported:
        await store.save_image(_png_bytes(), media_type="image/tiff")
    assert unsupported.value.code == UNSUPPORTED_IMAGE_TYPE


async def test_declared_media_type_accepts_jpg_alias(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_image_bytes("JPEG"), media_type="image/jpg")
    assert ref.mediaType == "image/png"


async def test_max_pixels_is_enforced_before_decode(tmp_path: Path) -> None:
    store = _store(tmp_path, maxPixels=20)
    with pytest.raises(AttachmentError) as info:
        await store.save_image(_png_bytes(size=(10, 5)))
    assert info.value.code == IMAGE_TOO_MANY_PIXELS


async def test_max_dimension_is_enforced_before_decode(tmp_path: Path) -> None:
    store = _store(tmp_path, maxDimension=8)
    with pytest.raises(AttachmentError) as info:
        await store.save_image(_png_bytes(size=(10, 5)))
    assert info.value.code == IMAGE_DIMENSION_TOO_LARGE


async def test_payload_over_byte_limit_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path, maxImageBytes=10)
    with pytest.raises(AttachmentError) as info:
        await store.save_image(_png_bytes())
    assert info.value.code == IMAGES_TOO_LARGE


async def test_read_missing_file_raises_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes())
    store.image_host_path(ref).unlink()
    with pytest.raises(AttachmentError) as info:
        store.read_image_bytes(ref)
    assert info.value.code == ATTACHMENT_NOT_FOUND


async def test_read_detects_truncated_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes())
    path = store.image_host_path(ref)
    path.write_bytes(path.read_bytes()[: ref.bytes // 2])
    with pytest.raises(AttachmentError) as info:
        store.read_image_bytes(ref)
    assert info.value.code == ATTACHMENT_CORRUPT


async def test_read_detects_tampered_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes())
    path = store.image_host_path(ref)
    data = bytearray(path.read_bytes())
    data[-1] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(AttachmentError) as info:
        store.read_image_bytes(ref)
    assert info.value.code == ATTACHMENT_CORRUPT


async def test_read_detects_missing_manifest_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = await store.save_image(_png_bytes())
    store.manifest_path.write_text("\n", encoding="utf-8")
    with pytest.raises(AttachmentError) as info:
        store.read_image_bytes(ref)
    assert info.value.code == ATTACHMENT_CORRUPT


async def test_image_host_path_rejects_invalid_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    ref = ImageAttachmentRef(
        attachmentId="att-zzzzzzzzzzzzzzzz",
        mediaType="image/png",
        bytes=1,
        width=1,
        height=1,
    )
    with pytest.raises(AttachmentError) as info:
        store.image_host_path(ref)
    assert info.value.code == ATTACHMENT_NOT_FOUND


async def test_validate_image_batch(tmp_path: Path) -> None:
    def ref(size: int, index: int = 0) -> ImageAttachmentRef:
        return ImageAttachmentRef(
            attachmentId=f"att-{index:016x}",
            mediaType="image/png",
            bytes=size,
            width=1,
            height=1,
        )

    store = _store(tmp_path, maxImagesPerMessage=1, maxImageBytes=100)
    store.validate_image_batch([])
    store.validate_image_batch([ref(10)])
    with pytest.raises(AttachmentError) as too_many:
        store.validate_image_batch([ref(10), ref(10, 1)])
    assert too_many.value.code == TOO_MANY_IMAGES
    store = _store(tmp_path / "bytes", maxImageBytes=100)
    with pytest.raises(AttachmentError) as too_large:
        store.validate_image_batch([ref(60), ref(60, 1)])
    assert too_large.value.code == IMAGES_TOO_LARGE


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ConfigError):
        AttachmentConfig.from_config({"maxImagesPerMessage": 0})
    with pytest.raises(ConfigError):
        AttachmentConfig.from_config({"unknown": 1})
    with pytest.raises(ConfigError):
        AttachmentConfig.from_config(["not", "a", "table"])
    assert AttachmentConfig.from_config({"maxPixels": 5}).max_pixels == 5
    assert AttachmentConfig.from_config({"max_pixels": 7}).max_pixels == 7
    assert AttachmentConfig.from_config(AttachmentConfig(max_pixels=9)).max_pixels == 9
