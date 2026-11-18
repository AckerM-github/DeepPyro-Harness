"""Optional bundle HMAC-SHA256 signatures (KI-021).

A bundle.toml may declare ``[bundle].signature = "hmac-sha256:<hex>"``, an
HMAC-SHA256 over the file's raw bytes with a shared secret supplied through
``DEEPPYRO_BUNDLE_SIGNING_KEY`` or ``DEEPPYRO_BUNDLE_SIGNING_KEY_FILE``.
The signed payload is the file's bytes with the declared signature value
replaced by an all-zero placeholder (a value cannot sign itself); the declared
digest, if any, is *not* normalised, so a signature also binds the digest pin.
Unsigned bundles keep booting and are only recorded; a signed bundle is fatal
when the key is missing, the signature is malformed or the bytes do not match.
This is integrity/authenticity against a shared secret, not an OS sandbox.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError

from deeppyro.config import Config
from deeppyro.kernel.app import KernelBootError, create_app
from deeppyro.kernel.bundle_schema import BundleDocument
from deeppyro.kernel.capabilities import (
    BUNDLE_SIGNING_KEY_ENV,
    BUNDLE_SIGNING_KEY_FILE_ENV,
    SIGNATURE_PLACEHOLDER,
    BundleSignature,
    BundleSignatureState,
    BundleSigningKeyError,
    bundle_signature_bytes,
    bundle_signature_payload,
    inspect_bundle_signature,
    inspect_bundle_signatures,
    resolve_bundle_signing_key,
)
from deeppyro.kernel.composition import load_bundle

PROBE_ID = "signature-probe"
KEY = "correct horse battery staple"


def _config() -> Config:
    return Config.model_validate({"agent": {"provider": "local", "model": "m"}})


def _bundle_text(
    *,
    bundle_id: str = PROBE_ID,
    digest: str | None = None,
    signature: str | None = None,
) -> str:
    lines = ["[bundle]", f'id = "{bundle_id}"', 'name = "signature probe"']
    if digest is not None:
        lines.append(f'digest = "{digest}"')
    if signature is not None:
        lines.append(f'signature = "{signature}"')
    return "\n".join(lines) + "\n"


def _write_bundle(directory: Path, text: str) -> Path:
    # Bytes, not text: the signature covers the raw bytes, so the test must
    # control line endings (text mode would rewrite \n as \r\n on Windows).
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bundle.toml"
    path.write_bytes(text.encode("utf-8"))
    return path


def _sign(directory: Path, *, key: str = KEY, bundle_id: str = PROBE_ID) -> tuple[Path, str]:
    """Write a signed bundle, computing the signature before it contains it."""
    placeholder_text = _bundle_text(bundle_id=bundle_id, signature=SIGNATURE_PLACEHOLDER)
    signature = bundle_signature_bytes(
        placeholder_text.encode("utf-8"),
        key,
        declared=SIGNATURE_PLACEHOLDER,
    )
    return _write_bundle(directory, placeholder_text.replace(SIGNATURE_PLACEHOLDER, signature)), signature


def _pin_and_sign(
    directory: Path,
    *,
    key: str = KEY,
    bundle_id: str = PROBE_ID,
) -> tuple[Path, str, str]:
    """Write a bundle that both pins its sha256 digest and signs its bytes."""
    from deeppyro.kernel.capabilities import DIGEST_PLACEHOLDER, bundle_digest_bytes

    placeholder_text = _bundle_text(
        bundle_id=bundle_id,
        digest=DIGEST_PLACEHOLDER,
        signature=SIGNATURE_PLACEHOLDER,
    )
    digest = "sha256:" + bundle_digest_bytes(
        placeholder_text.encode("utf-8"),
        declared=DIGEST_PLACEHOLDER,
    )
    # Replace the placeholder inside its own assignment only: the signature
    # placeholder contains the digest placeholder as a substring, so a plain
    # str.replace would corrupt it.
    pinned_text = placeholder_text.replace(
        f'digest = "{DIGEST_PLACEHOLDER}"',
        f'digest = "{digest}"',
    )
    signature = bundle_signature_bytes(pinned_text.encode("utf-8"), key, declared=SIGNATURE_PLACEHOLDER)
    signed_text = pinned_text.replace(
        f'signature = "{SIGNATURE_PLACEHOLDER}"',
        f'signature = "{signature}"',
    )
    return _write_bundle(directory, signed_text), digest, signature


def test_bundle_schema_accepts_valid_signature_and_rejects_malformed() -> None:
    valid = "hmac-sha256:" + "b" * 64
    document = BundleDocument.model_validate(
        {"bundle": {"id": PROBE_ID, "signature": f" {valid} "}, "plugins": []}
    )
    assert document.bundle.signature == valid

    for malformed in ("hmac-sha256:ABC", "sha256:" + "0" * 64, "md5:" + "0" * 32):
        with pytest.raises(ValidationError, match="signature must be 'hmac-sha256:'"):
            BundleDocument.model_validate({"bundle": {"id": PROBE_ID, "signature": malformed}})


def test_signature_payload_normalises_only_the_declared_signature() -> None:
    data = _bundle_text(signature="hmac-sha256:" + "a" * 64).encode("utf-8")
    expected = _bundle_text(signature=SIGNATURE_PLACEHOLDER).encode("utf-8")

    assert bundle_signature_payload(data, declared="hmac-sha256:" + "a" * 64) == expected
    # A malformed declaration is never treated as the value to replace.
    assert bundle_signature_payload(data, declared="hmac-sha256:ABC") == data
    # Without a declared value nothing is normalised.
    assert bundle_signature_payload(data) == data


def test_signature_is_hmac_sha256_of_the_normalised_bytes() -> None:
    data = _bundle_text(signature=SIGNATURE_PLACEHOLDER).encode("utf-8")
    expected = (
        "hmac-sha256:"
        + hmac.new(
            KEY.encode("utf-8"),
            data,
            hashlib.sha256,
        ).hexdigest()
    )

    assert bundle_signature_bytes(data, KEY, declared=SIGNATURE_PLACEHOLDER) == expected
    assert bundle_signature_bytes(data, KEY.encode("utf-8"), declared=SIGNATURE_PLACEHOLDER) == expected
    # A different key or any byte change produces a different signature.
    assert bundle_signature_bytes(data, "other", declared=SIGNATURE_PLACEHOLDER) != expected
    assert bundle_signature_bytes(data + b"# note\n", KEY, declared=SIGNATURE_PLACEHOLDER) != expected


def test_signature_binds_the_declared_digest() -> None:
    from deeppyro.kernel.capabilities import DIGEST_PLACEHOLDER, bundle_digest_bytes

    placeholder = _bundle_text(
        digest=DIGEST_PLACEHOLDER,
        signature=SIGNATURE_PLACEHOLDER,
    ).encode("utf-8")
    digest = "sha256:" + bundle_digest_bytes(placeholder, declared=DIGEST_PLACEHOLDER)
    pinned = _bundle_text(digest=digest, signature=SIGNATURE_PLACEHOLDER).encode("utf-8")

    signed = bundle_signature_bytes(pinned, KEY, declared=SIGNATURE_PLACEHOLDER)
    swapped = _bundle_text(digest="sha256:" + "0" * 64, signature=SIGNATURE_PLACEHOLDER).encode("utf-8")
    assert bundle_signature_bytes(swapped, KEY, declared=SIGNATURE_PLACEHOLDER) != signed


def test_bundle_signing_key_resolution_prefers_inline_then_file(tmp_path: Path) -> None:
    assert resolve_bundle_signing_key({}) is None

    key_file = tmp_path / "key.txt"
    key_file.write_bytes(b"  file-secret\n")
    assert resolve_bundle_signing_key({BUNDLE_SIGNING_KEY_FILE_ENV: str(key_file)}) == "file-secret"
    assert (
        resolve_bundle_signing_key(
            {BUNDLE_SIGNING_KEY_ENV: "inline-secret", BUNDLE_SIGNING_KEY_FILE_ENV: str(key_file)}
        )
        == "inline-secret"
    )

    with pytest.raises(BundleSigningKeyError, match="cannot read"):
        resolve_bundle_signing_key({BUNDLE_SIGNING_KEY_FILE_ENV: str(tmp_path / "missing")})
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"   \n")
    with pytest.raises(BundleSigningKeyError, match="is empty"):
        resolve_bundle_signing_key({BUNDLE_SIGNING_KEY_FILE_ENV: str(empty)})


def test_inspect_bundle_signature_reports_each_state(tmp_path: Path) -> None:
    unsigned = _write_bundle(tmp_path / "unsigned", _bundle_text(bundle_id="unsigned"))
    invalid = _write_bundle(
        tmp_path / "invalid",
        _bundle_text(bundle_id="invalid", signature="hmac-sha256:ABC"),
    )
    signed, _signature = _sign(tmp_path / "signed", bundle_id="signed")
    wrong = _write_bundle(
        tmp_path / "wrong",
        _bundle_text(bundle_id="wrong", signature="hmac-sha256:" + "d" * 64),
    )

    assert inspect_bundle_signature(unsigned, key=KEY).state is BundleSignatureState.UNVERIFIED
    assert inspect_bundle_signature(invalid, key=KEY).state is BundleSignatureState.INVALID
    assert inspect_bundle_signature(wrong, key=KEY).state is BundleSignatureState.MISMATCH
    assert inspect_bundle_signature(signed, key=KEY).state is BundleSignatureState.MATCH
    # A declared signature without a configured key is fatal at boot, not a match.
    assert inspect_bundle_signature(signed, key=None).state is BundleSignatureState.MISSING_KEY
    # An unreadable path is unverified instead of raising (composition reports it).
    assert inspect_bundle_signature(tmp_path / "absent", key=KEY).state is BundleSignatureState.UNVERIFIED


def test_inspect_bundle_signatures_skips_missing_files(tmp_path: Path) -> None:
    signed, _signature = _sign(tmp_path / "signed")
    missing = tmp_path / "absent" / "bundle.toml"

    records = inspect_bundle_signatures([signed, missing], key=KEY)

    assert len(records) == 1
    assert records[0].state is BundleSignatureState.MATCH
    assert records[0].path.endswith("bundle.toml")


def test_signed_bundle_is_still_loaded_by_composition(tmp_path: Path) -> None:
    signed, _signature = _sign(tmp_path / "signed", bundle_id="signed")

    bundle = load_bundle(signed)

    assert bundle.id == "signed"
    assert inspect_bundle_signature(signed, key=KEY).state is BundleSignatureState.MATCH


def test_digest_and_signature_verify_together(tmp_path: Path) -> None:
    from deeppyro.kernel.capabilities import BundleDigestState, inspect_bundle_digest

    pinned, digest, signature = _pin_and_sign(tmp_path / "pinned", bundle_id="pinned")
    data = pinned.read_bytes()

    assert digest.encode("ascii") in data
    assert signature.encode("ascii") in data
    assert inspect_bundle_digest(pinned).state is BundleDigestState.MATCH
    assert inspect_bundle_signature(pinned, key=KEY).state is BundleSignatureState.MATCH

    # Tampering with either declared value or the payload flips both checks:
    # the digest pins the bytes, the signature binds the digest value too.
    tampered = pinned.with_name("bundle.toml")
    tampered.write_bytes(data.replace(b'name = "signature probe"', b'name = "tampered probe"'))
    assert inspect_bundle_digest(pinned).state is BundleDigestState.MISMATCH
    assert inspect_bundle_signature(pinned, key=KEY).state is BundleSignatureState.MISMATCH


def test_bundle_signature_record_is_frozen_and_serialisable() -> None:
    record = BundleSignature(
        path="bundle.toml",
        bundle_id=PROBE_ID,
        declared="hmac-sha256:" + "a" * 64,
        state=BundleSignatureState.MATCH,
    )

    assert record.to_dict() == {
        "path": "bundle.toml",
        "bundleId": PROBE_ID,
        "declared": "hmac-sha256:" + "a" * 64,
        "state": "match",
    }
    with pytest.raises(FrozenInstanceError):
        record.state = BundleSignatureState.MISMATCH  # type: ignore[misc]


async def _boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundle_dir: Path | None):
    monkeypatch.setenv("DEEPPYRO_HOME", str(tmp_path / "home"))
    bundle_paths = [str(bundle_dir)] if bundle_dir is not None else []
    return await create_app(_config(), bundle_paths=bundle_paths)


@pytest.fixture(autouse=True)
def _clean_signing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never inherit a signing key from the developer's machine."""
    monkeypatch.delenv(BUNDLE_SIGNING_KEY_ENV, raising=False)
    monkeypatch.delenv(BUNDLE_SIGNING_KEY_FILE_ENV, raising=False)
    monkeypatch.setenv("DEEPPYRO_HOME", str(Path.cwd() / ".pytest-signature-home"))


async def test_boot_records_unsigned_bundles_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, _bundle_text())

    app = await _boot(tmp_path, monkeypatch, bundle_dir)

    report = app.capability_report()
    record = report.signature(str(bundle_dir / "bundle.toml"))
    assert record is not None
    assert record.state is BundleSignatureState.UNVERIFIED
    assert record.declared is None
    assert report.ok
    assert any(item["state"] == "unverified" for item in app.capability_dict()["signatures"])


async def test_boot_accepts_correct_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    _sign(bundle_dir, bundle_id="signed")
    monkeypatch.setenv(BUNDLE_SIGNING_KEY_ENV, KEY)

    app = await _boot(tmp_path, monkeypatch, bundle_dir)

    record = app.capability_report().signature(str(bundle_dir / "bundle.toml"))
    assert record is not None
    assert record.bundle_id == "signed"
    assert record.state is BundleSignatureState.MATCH


async def test_boot_reads_the_key_from_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    _sign(bundle_dir, bundle_id="signed")
    key_file = tmp_path / "signing.key"
    key_file.write_bytes(f"  {KEY}\n".encode())
    monkeypatch.setenv(BUNDLE_SIGNING_KEY_FILE_ENV, str(key_file))

    app = await _boot(tmp_path, monkeypatch, bundle_dir)

    record = app.capability_report().signature("signed")
    assert record is not None
    assert record.state is BundleSignatureState.MATCH


async def test_boot_rejects_a_modified_signed_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    path, _signature = _sign(bundle_dir, bundle_id="signed")
    path.write_bytes(path.read_bytes() + b"# trailing comment\n")
    monkeypatch.setenv(BUNDLE_SIGNING_KEY_ENV, KEY)

    with pytest.raises(KernelBootError, match="refusing to boot"):
        await _boot(tmp_path, monkeypatch, bundle_dir)


async def test_boot_rejects_a_signed_bundle_without_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = tmp_path / "bundle"
    _sign(bundle_dir, bundle_id="signed")

    with pytest.raises(KernelBootError, match=BUNDLE_SIGNING_KEY_ENV):
        await _boot(tmp_path, monkeypatch, bundle_dir)


async def test_boot_rejects_a_signature_signed_with_another_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = tmp_path / "bundle"
    _sign(bundle_dir, bundle_id="signed")
    monkeypatch.setenv(BUNDLE_SIGNING_KEY_ENV, "a different shared secret")

    with pytest.raises(KernelBootError, match="HMAC-SHA256 signature"):
        await _boot(tmp_path, monkeypatch, bundle_dir)


async def test_boot_rejects_a_malformed_signature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_bundle(bundle_dir, _bundle_text(bundle_id="broken", signature="hmac-sha256:not-hex"))
    monkeypatch.setenv(BUNDLE_SIGNING_KEY_ENV, KEY)

    with pytest.raises(KernelBootError, match="expected 'hmac-sha256:'"):
        await _boot(tmp_path, monkeypatch, bundle_dir)


async def test_boot_rejects_an_unreadable_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle_dir = tmp_path / "bundle"
    _sign(bundle_dir, bundle_id="signed")
    monkeypatch.setenv(BUNDLE_SIGNING_KEY_FILE_ENV, str(tmp_path / "missing.key"))

    with pytest.raises(KernelBootError, match="cannot configure bundle signing"):
        await _boot(tmp_path, monkeypatch, bundle_dir)


async def test_boot_accepts_a_pinned_and_signed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from deeppyro.kernel.capabilities import BundleDigestState

    bundle_dir = tmp_path / "bundle"
    _pin_and_sign(bundle_dir, bundle_id="both")
    monkeypatch.setenv(BUNDLE_SIGNING_KEY_ENV, KEY)

    app = await _boot(tmp_path, monkeypatch, bundle_dir)

    report = app.capability_report()
    assert report.bundle("both") is not None
    assert report.bundle("both").state is BundleDigestState.MATCH  # type: ignore[union-attr]
    assert report.signature("both") is not None
    assert report.signature("both").state is BundleSignatureState.MATCH  # type: ignore[union-attr]
    assert report.ok
