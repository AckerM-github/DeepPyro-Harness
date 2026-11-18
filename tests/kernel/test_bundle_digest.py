"""Bundle content-digest pinning (KI-021b).

A bundle.toml may pin the sha256 of its own raw bytes with
``[bundle].digest = "sha256:..."``.  The hash covers the bytes as parsed from
disk, except that the declared digest value itself is replaced by an all-zero
placeholder of the same length (otherwise a pinned file could never match).
Unpinned bundles keep booting and are only recorded; a bad pin is fatal.  The
normalisation is user visible, so it is also documented in
docs/guides/bundle-digest.md.
"""

from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from deeppyro.config import Config
from deeppyro.kernel.app import KernelBootError, create_app
from deeppyro.kernel.bundle_schema import DIGEST_PREFIX, BundleDocument
from deeppyro.kernel.capabilities import (
    DIGEST_PLACEHOLDER,
    BundleDigest,
    BundleDigestState,
    bundle_digest_bytes,
    inspect_bundle_digest,
    inspect_bundle_digests,
)
from deeppyro.kernel.composition import CompositionError, load_bundle

PROBE_ID = "digest-probe"


def _config() -> Config:
    return Config.model_validate({"agent": {"provider": "local", "model": "m"}})


def _bundle_text(*, bundle_id: str = PROBE_ID, digest: str | None = None) -> str:
    lines = ["[bundle]", f'id = "{bundle_id}"', 'name = "digest probe"']
    if digest is not None:
        lines.append(f'digest = "{digest}"')
    return "\n".join(lines) + "\n"


def _write_bundle(directory: Path, text: str) -> Path:
    # Bytes, not text: the digest covers the raw bytes, so the test must control
    # line endings (text mode would rewrite \n as \r\n on Windows).
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bundle.toml"
    path.write_bytes(text.encode("utf-8"))
    return path


def _pin(directory: Path, *, bundle_id: str = PROBE_ID) -> tuple[Path, str]:
    """Write a pinned bundle, computing the hash before it contains the hash."""
    placeholder_text = _bundle_text(bundle_id=bundle_id, digest=DIGEST_PLACEHOLDER)
    digest = bundle_digest_bytes(placeholder_text.encode("utf-8"), declared=DIGEST_PLACEHOLDER)
    pinned = f"{DIGEST_PREFIX}{digest}"
    return _write_bundle(directory, placeholder_text.replace(DIGEST_PLACEHOLDER, pinned)), pinned


def test_digest_hashes_raw_bytes_without_toml_normalisation() -> None:
    text = _bundle_text()
    data = text.encode("utf-8")

    assert bundle_digest_bytes(data) == hashlib.sha256(data).hexdigest()
    assert bundle_digest_bytes(data + b"# trailing comment\n") != bundle_digest_bytes(data)
    assert bundle_digest_bytes(b"\xef\xbb\xbf" + data) != bundle_digest_bytes(data)


def test_declared_digest_is_replaced_by_a_zero_placeholder() -> None:
    placeholder = _bundle_text(digest=DIGEST_PLACEHOLDER).encode("utf-8")
    expected = hashlib.sha256(placeholder).hexdigest()
    digest = "sha256:" + "a" * 64
    declared = _bundle_text(digest=digest).encode("utf-8")

    assert bundle_digest_bytes(declared, declared=digest) == expected
    # A malformed declaration is never treated as the value to replace.
    assert bundle_digest_bytes(declared, declared="sha256:ABC") == hashlib.sha256(declared).hexdigest()


def test_bundle_schema_accepts_valid_digest_and_rejects_malformed_pins() -> None:
    valid = "sha256:" + "b" * 64
    document = BundleDocument.model_validate(
        {"bundle": {"id": PROBE_ID, "digest": f" {valid} "}, "plugins": []}
    )
    assert document.bundle.digest == valid

    with pytest.raises(ValidationError, match="digest must be 'sha256:'"):
        BundleDocument.model_validate({"bundle": {"id": PROBE_ID, "digest": "sha256:ABC"}})
    with pytest.raises(ValidationError, match="digest must be 'sha256:'"):
        BundleDocument.model_validate({"bundle": {"id": PROBE_ID, "digest": "md5:" + "0" * 32}})


def test_inspect_bundle_digest_reports_each_state(tmp_path: Path) -> None:
    unpinned = _write_bundle(tmp_path / "unpinned", _bundle_text(bundle_id="unpinned"))
    invalid = _write_bundle(
        tmp_path / "invalid", _bundle_text(bundle_id="invalid", digest="SHA256:not-a-digest")
    )
    wrong = _write_bundle(tmp_path / "wrong", _bundle_text(bundle_id="wrong", digest="sha256:" + "c" * 64))
    pinned, digest = _pin(tmp_path / "pinned", bundle_id="pinned")

    assert inspect_bundle_digest(unpinned).state is BundleDigestState.UNVERIFIED
    assert inspect_bundle_digest(invalid).state is BundleDigestState.INVALID
    assert inspect_bundle_digest(wrong).state is BundleDigestState.MISMATCH
    matched = inspect_bundle_digest(pinned)
    assert matched.state is BundleDigestState.MATCH
    assert matched.declared == digest
    assert matched.bundle_id == "pinned"
    assert matched.sha256 == digest.removeprefix(DIGEST_PREFIX)
    assert matched.to_dict()["bundleId"] == "pinned"

    # Directories resolve to their bundle.toml; paths without a file are skipped.
    batch = inspect_bundle_digests([tmp_path / "unpinned", tmp_path / "missing", pinned])
    assert [item.bundle_id for item in batch] == ["unpinned", "pinned"]


def test_pinned_bundle_still_loads_through_the_composition(tmp_path: Path) -> None:
    pinned, digest = _pin(tmp_path / "bundle")
    bundle = load_bundle(pinned)
    assert bundle.id == PROBE_ID
    assert bundle.path == pinned

    malformed = _write_bundle(tmp_path / "malformed", _bundle_text(digest="sha256:short"))
    with pytest.raises(CompositionError, match="digest must be"):
        load_bundle(malformed)
    assert digest.startswith(DIGEST_PREFIX)


async def _boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle_dir: Path):
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    return await create_app(_config(), bundle_paths=[str(bundle_dir)])


async def test_boot_records_unverified_digest_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, _bundle_text(bundle_id="unpinned"))
    app = await _boot(tmp_path, monkeypatch, bundle_dir)
    try:
        report = app.capability_report()
        record = report.bundle("unpinned")
        assert record is not None
        assert record.state is BundleDigestState.UNVERIFIED
        assert record.declared is None
        assert len(record.sha256) == 64
        assert record.path.endswith("bundle.toml")
        assert report.ok
        assert app.capability_dict()["bundles"]
    finally:
        await app.stop()


async def test_boot_accepts_matching_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    _, digest = _pin(bundle_dir, bundle_id="pinned")
    app = await _boot(tmp_path, monkeypatch, bundle_dir)
    try:
        report = app.capability_report()
        record = report.bundle("pinned")
        assert record is not None
        assert record.state is BundleDigestState.MATCH
        assert record.declared == digest
        assert report.ok
        dicts = {item["bundleId"]: item for item in app.capability_dict()["bundles"]}
        assert dicts["pinned"]["state"] == "match"
        assert dicts["pinned"]["declared"] == digest
    finally:
        await app.stop()


async def test_boot_refuses_mismatched_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    pinned, digest = _pin(bundle_dir, bundle_id="pinned")
    # Flip the pinned digest after the fact: the bytes no longer hash to it.
    wrong = "sha256:" + ("d" * 64 if digest.removeprefix(DIGEST_PREFIX)[0] != "d" else "e" * 64)
    pinned.write_bytes(pinned.read_bytes().replace(digest.encode("ascii"), wrong.encode("ascii")))

    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    with pytest.raises(KernelBootError) as excinfo:
        await create_app(_config(), bundle_paths=[str(bundle_dir)])
    message = str(excinfo.value)
    assert "pinned" in message
    assert "refusing to boot" in message
    assert wrong in message


async def test_boot_refuses_malformed_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, _bundle_text(bundle_id="broken", digest="sha256:tooshort"))

    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    with pytest.raises(KernelBootError) as excinfo:
        await create_app(_config(), bundle_paths=[str(bundle_dir)])
    message = str(excinfo.value)
    assert "broken" in message
    assert "expected 'sha256:'" in message


def test_bundle_digest_is_frozen_and_serialisable() -> None:
    record = BundleDigest(path="x/bundle.toml", bundle_id="x", sha256="0" * 64)
    with pytest.raises(FrozenInstanceError):
        record.path = "y"  # type: ignore[misc]
    assert record.to_dict()["state"] == "unverified"
