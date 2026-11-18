"""Kernel capability registry: derived manifests, dependency checks, reports.

Every composition row and every host-style plugin mounted through
``ctx.mount`` contributes a :class:`~deeppyro.api.capabilities.CapabilityManifest`
to the registry.  The kernel uses it at boot to reject duplicate service
providers (naming both plugin ids) and version-mismatched requirements, and to
report unresolved requirements as diagnostics (a missing dependency leaves the
dependent fiber PENDING rather than aborting the whole boot, see
`deeppyro.kernel.fiber`).  The plugin CLI renders the same data.

The registry also snapshots two kinds of host facts that are not capability
declarations but affect what a boot can promise:

* bundle digests: the sha256 of every bundle.toml the default composition
  loaded, plus the optional `[bundle].digest` pin.  An unpinned bundle is
  recorded as ``unverified``; a wrong or malformed pin is a fatal issue (see
  docs/guides/bundle-digest.md).
* bundle signatures: the optional `[bundle].signature` HMAC-SHA256 of the same
  bytes, using a shared secret supplied through the environment.  An unsigned
  bundle is recorded as ``unverified``; a signed bundle is fatal when the key
  is missing, the signature is malformed or the bytes do not match (see
  docs/guides/bundle-signing.md).  This proves integrity/authenticity for a
  shared secret; it is not an OS sandbox.
* the host sandbox boundary: which enforcement level the local sandbox backend
  can actually deliver (see docs/guides/windows-sandbox-boundaries.md).  The
  live per-command value still comes from
  `deeppyro.sandbox.provider.Confinement`; this record only makes the host-wide
  limit visible to capability report consumers.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from deeppyro.api.capabilities import CapabilityManifest, build_manifest
from deeppyro.kernel.bundle_schema import (
    DIGEST_PATTERN,
    DIGEST_PREFIX,
    SIGNATURE_PATTERN,
    SIGNATURE_PREFIX,
)
from deeppyro.kernel.composition import (
    Composition,
    PluginRow,
    resolve_row,
    version_satisfies,
)


class CapabilityIssueKind(StrEnum):
    """What is wrong with the declared capability graph."""

    DUPLICATE_PROVIDE = "duplicate-provide"
    MISSING_REQUIRE = "missing-require"
    PENDING_PROVIDER = "pending-provider"
    VERSION_MISMATCH = "version-mismatch"
    INVALID_MANIFEST = "invalid-manifest"
    BUNDLE_DIGEST_MISMATCH = "bundle-digest-mismatch"
    INVALID_BUNDLE_DIGEST = "invalid-bundle-digest"
    BUNDLE_SIGNATURE_MISMATCH = "bundle-signature-mismatch"
    INVALID_BUNDLE_SIGNATURE = "invalid-bundle-signature"
    BUNDLE_SIGNATURE_KEY_MISSING = "bundle-signature-key-missing"


#: Issue kinds that refuse boot: two live providers of one service, an
#: unsatisfiable version requirement, or a bundle whose bytes do not match the
#: digest/signature it declares (or whose signature cannot be verified because
#: no key is configured).
FATAL_ISSUES: frozenset[CapabilityIssueKind] = frozenset(
    {
        CapabilityIssueKind.DUPLICATE_PROVIDE,
        CapabilityIssueKind.VERSION_MISMATCH,
        CapabilityIssueKind.BUNDLE_DIGEST_MISMATCH,
        CapabilityIssueKind.INVALID_BUNDLE_DIGEST,
        CapabilityIssueKind.BUNDLE_SIGNATURE_MISMATCH,
        CapabilityIssueKind.INVALID_BUNDLE_SIGNATURE,
        CapabilityIssueKind.BUNDLE_SIGNATURE_KEY_MISSING,
    }
)

#: Services the kernel provides before any composition row is mounted.
IMPLICIT_SERVICES: frozenset[str] = frozenset({"config"})

#: Substitute written before hashing so a bundle.toml can pin its own digest.
#: Same length as a real digest ("sha256:" + 64 hex chars), so the substitution
#: never shifts the rest of the file.
DIGEST_PLACEHOLDER = DIGEST_PREFIX + "0" * 64
#: Substitute written before hashing so a bundle.toml can carry its own
#: signature.  Same length as a real signature ("hmac-sha256:" + 64 hex chars).
SIGNATURE_PLACEHOLDER = SIGNATURE_PREFIX + "0" * 64

#: Environment variable holding the shared HMAC secret inline.
BUNDLE_SIGNING_KEY_ENV = "DEEPPYRO_BUNDLE_SIGNING_KEY"
#: Environment variable naming a file that holds the shared HMAC secret.
BUNDLE_SIGNING_KEY_FILE_ENV = "DEEPPYRO_BUNDLE_SIGNING_KEY_FILE"

ENTRY_ENABLED = "enabled"
ENTRY_DISABLED = "disabled"
ENTRY_GROUP = "group"
ENTRY_RUNTIME = "runtime"


@dataclass(frozen=True, slots=True)
class CapabilityIssue:
    """One diagnostic about the declared capability graph."""

    kind: CapabilityIssueKind
    plugin_id: str
    message: str
    related: tuple[str, ...] = ()

    @property
    def fatal(self) -> bool:
        """Whether this issue refuses boot."""
        return self.kind in FATAL_ISSUES

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "kind": str(self.kind),
            "pluginId": self.plugin_id,
            "message": self.message,
            "related": list(self.related),
            "fatal": self.fatal,
        }


@dataclass(frozen=True, slots=True)
class CapabilityEntry:
    """One manifest registered with the kernel plus its mount bookkeeping."""

    plugin_id: str
    manifest: CapabilityManifest
    origin: str = ""
    state: str = ENTRY_ENABLED
    isolate: tuple[str, ...] = ()
    active: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "pluginId": self.plugin_id,
            "origin": self.origin,
            "state": self.state,
            "isolate": list(self.isolate),
            "active": self.active,
            "manifest": self.manifest.to_dict(),
        }


class BundleDigestState(StrEnum):
    """How a loaded bundle's bytes compare with its optional digest pin."""

    #: No ``[bundle].digest`` was declared; the hash is recorded but never blocks.
    UNVERIFIED = "unverified"
    #: The declared digest matches the normalised bytes.
    MATCH = "match"
    #: The declared digest is valid but describes different bytes.
    MISMATCH = "mismatch"
    #: The declared digest is not ``sha256:`` plus 64 lowercase hex characters.
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class BundleDigest:
    """The sha256 of one loaded bundle.toml plus its optional declared pin."""

    path: str
    bundle_id: str = ""
    sha256: str = ""
    declared: str | None = None
    state: BundleDigestState = BundleDigestState.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "path": self.path,
            "bundleId": self.bundle_id,
            "sha256": self.sha256,
            "declared": self.declared,
            "state": str(self.state),
        }


class BundleSignatureState(StrEnum):
    """How a loaded bundle's bytes compare with its optional HMAC signature."""

    #: No ``[bundle].signature`` was declared; nothing is verified.
    UNVERIFIED = "unverified"
    #: A signature was declared and matches the normalised bytes and key.
    MATCH = "match"
    #: A signature was declared and is valid, but the bytes/key do not match it.
    MISMATCH = "mismatch"
    #: The declared signature is not ``hmac-sha256:`` plus 64 lowercase hex characters.
    INVALID = "invalid"
    #: A signature was declared but no signing key is configured.
    MISSING_KEY = "missing-key"


@dataclass(frozen=True, slots=True)
class BundleSignature:
    """The declared HMAC signature of one loaded bundle.toml plus its state."""

    path: str
    bundle_id: str = ""
    declared: str | None = None
    state: BundleSignatureState = BundleSignatureState.UNVERIFIED

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "path": self.path,
            "bundleId": self.bundle_id,
            "declared": self.declared,
            "state": str(self.state),
        }


@dataclass(frozen=True, slots=True)
class SandboxBoundary:
    """Host-wide sandbox enforcement facts that capability consumers can trust.

    ``enforcement`` mirrors ``Confinement.enforcement`` from
    `deeppyro.sandbox.provider`: ``full`` (OS-enforced), ``partial`` (Windows
    ACL restricted token: file writes only) or ``unavailable`` (no backend, so
    ``read-only``/``workspace-write`` fail closed).  ``reason`` is a short
    plain-text justification for the value.
    """

    platform: str
    enforcement: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """Render as plain JSON-compatible data."""
        return {
            "platform": self.platform,
            "enforcement": self.enforcement,
            "reason": self.reason,
        }


def host_sandbox_boundary() -> SandboxBoundary:
    """Describe this host's local sandbox backend without probing it.

    Values mirror `deeppyro.sandbox.provider.LocalSandboxProvider`: Windows
    reports ``partial`` and every other platform reports ``unavailable``.
    This is a report fact, not a policy switch: per-command enforcement still
    comes from the sandbox plugin's live ``Confinement``.
    """
    if os.name == "nt":
        return SandboxBoundary(
            platform="windows",
            enforcement="partial",
            reason=(
                "the Windows ACL restricted-token backend confines file writes only: "
                "workspace and private-temp writes are limited by ACLs granted to a synthetic "
                "SID, while network access, reads, and process/registry operations are not "
                "constrained; danger-full-access runs unsandboxed"
            ),
        )
    return SandboxBoundary(
        platform=os.name,
        enforcement="unavailable",
        reason=(
            "no local sandbox backend is implemented on this platform, so read-only and "
            "workspace-write fail closed; only danger-full-access runs, unsandboxed"
        ),
    )


def _bundle_toml_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate / "bundle.toml" if candidate.is_dir() else candidate


def bundle_digest_bytes(
    data: bytes,
    *,
    declared: str | None = None,
    signature: str | None = None,
) -> str:
    """sha256 of bundle.toml bytes, normalising valid pins to placeholders.

    The hash always covers the bytes as parsed from disk (BOM included) and TOML
    is never re-serialised, so comments, key order and whitespace are part of the
    hash.  When ``declared`` or ``signature`` is syntactically valid its first
    verbatim occurrence is replaced by the matching all-zero placeholder before
    hashing: without those substitutions a pinned/signed file could never match,
    because the digest would have to contain itself.  Both self-references are
    normalised so a bundle can carry a digest and a signature independently in
    either order.  See docs/guides/bundle-digest.md.
    """
    payload = data
    if declared is not None and DIGEST_PATTERN.fullmatch(declared):
        needle = declared.encode("ascii")
        if needle in payload:
            payload = payload.replace(needle, DIGEST_PLACEHOLDER.encode("ascii"), 1)
    if signature is not None and SIGNATURE_PATTERN.fullmatch(signature):
        needle = signature.encode("ascii")
        if needle in payload:
            payload = payload.replace(needle, SIGNATURE_PLACEHOLDER.encode("ascii"), 1)
    return hashlib.sha256(payload).hexdigest()


def _read_bundle_declarations(data: bytes) -> tuple[str | None, str | None, str]:
    """Return (declared digest, declared signature, bundle id) from raw bytes."""
    payload = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, None, ""
    table = document.get("bundle")
    if not isinstance(table, dict):
        return None, None, ""
    raw_id = table.get("id")
    bundle_id = raw_id.strip() if isinstance(raw_id, str) else ""
    raw_digest = table.get("digest")
    digest = raw_digest.strip() if isinstance(raw_digest, str) else None
    raw_signature = table.get("signature")
    signature = raw_signature.strip() if isinstance(raw_signature, str) else None
    return digest, signature, bundle_id


def inspect_bundle_digest(path: str | Path) -> BundleDigest:
    """Read one bundle.toml and record its hash plus declared-digest state.

    Accepts a bundle.toml file or a directory containing it.  An unreadable or
    unparseable file is reported as ``unverified`` with an empty ``sha256``
    instead of raising: composition load already reports the real error (a
    warning for optional bundles, a boot error for the core bundle).
    """
    bundle_path = _bundle_toml_path(path)
    try:
        data = bundle_path.read_bytes()
    except OSError:
        return BundleDigest(path=str(bundle_path))
    declared, signature, bundle_id = _read_bundle_declarations(data)
    sha256 = bundle_digest_bytes(data, declared=declared, signature=signature)
    if declared is None:
        state = BundleDigestState.UNVERIFIED
    elif not DIGEST_PATTERN.fullmatch(declared):
        state = BundleDigestState.INVALID
    elif f"{DIGEST_PREFIX}{sha256}" == declared:
        state = BundleDigestState.MATCH
    else:
        state = BundleDigestState.MISMATCH
    return BundleDigest(
        path=str(bundle_path),
        bundle_id=bundle_id,
        sha256=sha256,
        declared=declared,
        state=state,
    )


def inspect_bundle_digests(paths: Iterable[str | Path]) -> tuple[BundleDigest, ...]:
    """Inspect every path that resolves to an existing bundle.toml."""
    digests: list[BundleDigest] = []
    for path in paths:
        bundle_path = _bundle_toml_path(path)
        if not bundle_path.is_file():
            continue
        digests.append(inspect_bundle_digest(bundle_path))
    return tuple(digests)


class BundleSigningKeyError(ValueError):
    """A configured bundle signing key source exists but cannot be used."""


def resolve_bundle_signing_key(environ: Mapping[str, str] | None = None) -> str | None:
    """Return the shared HMAC secret configured for this boot, if any.

    DEEPPYRO_BUNDLE_SIGNING_KEY wins and is used verbatim; otherwise
    DEEPPYRO_BUNDLE_SIGNING_KEY_FILE names a UTF-8 file whose content is
    stripped of surrounding whitespace.  A configured but unreadable or empty
    key file raises BundleSigningKeyError instead of booting with a silently
    disabled signature check.
    """
    env = os.environ if environ is None else environ
    inline = env.get(BUNDLE_SIGNING_KEY_ENV)
    if inline:
        return inline
    file_name = env.get(BUNDLE_SIGNING_KEY_FILE_ENV)
    if not file_name:
        return None
    path = Path(file_name).expanduser()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise BundleSigningKeyError(f"cannot read {BUNDLE_SIGNING_KEY_FILE_ENV} file {path}: {exc}") from exc
    try:
        key = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise BundleSigningKeyError(
            f"{BUNDLE_SIGNING_KEY_FILE_ENV} file {path} is not valid UTF-8: {exc}"
        ) from exc
    if not key:
        raise BundleSigningKeyError(f"{BUNDLE_SIGNING_KEY_FILE_ENV} file {path} is empty")
    return key


def _key_bytes(key: str | bytes) -> bytes:
    return key if isinstance(key, bytes) else key.encode("utf-8")


def bundle_signature_payload(data: bytes, *, declared: str | None = None) -> bytes:
    """Raw bytes covered by a signature: the declared signature normalised away."""
    payload = data
    if declared is not None and SIGNATURE_PATTERN.fullmatch(declared):
        needle = declared.encode("ascii")
        if needle in payload:
            payload = payload.replace(needle, SIGNATURE_PLACEHOLDER.encode("ascii"), 1)
    return payload


def bundle_signature_bytes(data: bytes, key: str | bytes, *, declared: str | None = None) -> str:
    """HMAC-SHA256 of bundle.toml bytes, rendered as hmac-sha256:<hex>.

    declared is the signature value currently present in data (the all-zero
    placeholder while producing a signature, the real value while verifying);
    it is replaced by an all-zero placeholder before hashing so the value does
    not sign itself.  The declared digest is *not* normalised, so a signature
    also binds the digest declaration.  A missing key raises
    BundleSigningKeyError.
    """
    if key is None:
        raise BundleSigningKeyError("no bundle signing key was supplied")
    mac = hmac.new(_key_bytes(key), bundle_signature_payload(data, declared=declared), hashlib.sha256)
    return f"{SIGNATURE_PREFIX}{mac.hexdigest()}"


def inspect_bundle_signature(
    path: str | Path,
    *,
    key: str | bytes | None,
) -> BundleSignature:
    """Read one bundle.toml and record its optional signature state.

    Accepts a bundle.toml file or a directory containing it.  An unreadable or
    unparseable file is reported as unverified instead of raising: composition
    load already reports the real error.  A signature is only verified when the
    file declares one; a declared signature without a key is missing-key (fatal
    at boot, see CapabilityRegistry.record_bundle_signatures).
    """
    bundle_path = _bundle_toml_path(path)
    try:
        data = bundle_path.read_bytes()
    except OSError:
        return BundleSignature(path=str(bundle_path))
    _digest, declared, bundle_id = _read_bundle_declarations(data)
    if declared is None:
        state = BundleSignatureState.UNVERIFIED
    elif not SIGNATURE_PATTERN.fullmatch(declared):
        state = BundleSignatureState.INVALID
    elif key is None:
        state = BundleSignatureState.MISSING_KEY
    elif hmac.compare_digest(bundle_signature_bytes(data, key, declared=declared), declared):
        state = BundleSignatureState.MATCH
    else:
        state = BundleSignatureState.MISMATCH
    return BundleSignature(
        path=str(bundle_path),
        bundle_id=bundle_id,
        declared=declared,
        state=state,
    )


def inspect_bundle_signatures(
    paths: Iterable[str | Path],
    *,
    key: str | bytes | None,
) -> tuple[BundleSignature, ...]:
    """Inspect every path that resolves to an existing bundle.toml."""
    signatures: list[BundleSignature] = []
    for path in paths:
        bundle_path = _bundle_toml_path(path)
        if not bundle_path.is_file():
            continue
        signatures.append(inspect_bundle_signature(bundle_path, key=key))
    return tuple(signatures)


@dataclass(frozen=True, slots=True)
class CapabilityReport:
    """Immutable snapshot of the registry: entries, issues and dependency trees."""

    entries: tuple[CapabilityEntry, ...] = ()
    issues: tuple[CapabilityIssue, ...] = ()
    available_services: frozenset[str] = frozenset()
    bundles: tuple[BundleDigest, ...] = ()
    signatures: tuple[BundleSignature, ...] = ()
    sandbox: SandboxBoundary = field(default_factory=host_sandbox_boundary)

    @property
    def ok(self) -> bool:
        """Whether the graph has no fatal issue."""
        return not any(issue.fatal for issue in self.issues)

    @property
    def fatal_issues(self) -> tuple[CapabilityIssue, ...]:
        """Issues that make the composition invalid."""
        return tuple(issue for issue in self.issues if issue.fatal)

    def entry(self, plugin_id: str) -> CapabilityEntry | None:
        """The first enabled/runtime entry for `plugin_id`, if registered."""
        for wanted in (ENTRY_ENABLED, ENTRY_RUNTIME):
            for entry in self.entries:
                if entry.plugin_id == plugin_id and entry.state == wanted:
                    return entry
        for entry in self.entries:
            if entry.plugin_id == plugin_id:
                return entry
        return None

    def entries_for(self, plugin_id: str) -> tuple[CapabilityEntry, ...]:
        """Every entry registered for `plugin_id`."""
        return tuple(entry for entry in self.entries if entry.plugin_id == plugin_id)

    def issues_for(self, plugin_id: str) -> tuple[CapabilityIssue, ...]:
        """Issues attached to `plugin_id` (as the plugin or a related provider)."""
        return tuple(
            issue for issue in self.issues if issue.plugin_id == plugin_id or plugin_id in issue.related
        )

    def provider(self, service: str) -> CapabilityEntry | None:
        """The enabled/runtime entry providing `service`, if any."""
        for entry in self.entries:
            if entry.state in (ENTRY_ENABLED, ENTRY_RUNTIME) and service in entry.manifest.provides:
                return entry
        return None

    def bundle(self, identifier: str) -> BundleDigest | None:
        """Recorded digest for a bundle path or bundle id, if inspected."""
        for digest in self.bundles:
            if identifier in (digest.path, digest.bundle_id):
                return digest
        return None

    def signature(self, identifier: str) -> BundleSignature | None:
        """Recorded signature state for a bundle path or bundle id, if inspected."""
        for signature in self.signatures:
            if identifier in (signature.path, signature.bundle_id):
                return signature
        return None

    def missing_dependencies(self, plugin_id: str) -> tuple[str, ...]:
        """Required service names of `plugin_id` that no live or declared entry provides."""
        entry = self.entry(plugin_id)
        if entry is None:
            return ()
        provided = set(entry.manifest.provides) | set(self.available_services)
        declared = {service for item in self.entries for service in item.manifest.provides}
        missing = [
            service
            for service in entry.manifest.requires
            if service not in provided and service not in declared
        ]
        return tuple(missing)

    def dependency_tree(self, plugin_id: str) -> dict[str, Any]:
        """Nested dependency tree rooted at `plugin_id` (cycles are cut)."""
        return self._tree(plugin_id, frozenset())

    def _tree(self, plugin_id: str, seen: frozenset[str]) -> dict[str, Any]:
        entry = self.entry(plugin_id)
        if entry is None:
            return {
                "pluginId": plugin_id,
                "version": None,
                "provides": [],
                "requires": [],
                "missing": True,
            }
        manifest = entry.manifest
        requires: list[dict[str, Any]] = []
        for service in manifest.requires:
            constraint = manifest.version_requirement(service)
            provider = self.provider(service)
            node: dict[str, Any] = {
                "service": service,
                "versionConstraint": constraint or None,
                "provider": None,
                "providerVersion": None,
                "satisfied": service in manifest.provides or service in self.available_services,
                "requires": [],
            }
            if provider is not None:
                version_ok = (
                    True if not constraint else version_satisfies(provider.manifest.version, constraint)
                )
                node["provider"] = provider.plugin_id
                node["providerVersion"] = provider.manifest.version
                node["satisfied"] = bool(node["satisfied"] or version_ok)
                if version_ok and provider.plugin_id not in seen:
                    nested = self._tree(provider.plugin_id, seen | {plugin_id})
                    node["requires"] = nested["requires"]
            elif node["satisfied"]:
                node["provider"] = "<host>"
            requires.append(node)
        return {
            "pluginId": entry.plugin_id,
            "version": manifest.version,
            "provides": list(manifest.provides),
            "requires": requires,
            "missing": False,
        }

    def to_dict(self) -> dict[str, Any]:
        """Render the whole report as plain JSON-compatible data."""
        return {
            "ok": self.ok,
            "issues": [issue.to_dict() for issue in self.issues],
            "manifests": [
                {
                    "pluginId": entry.plugin_id,
                    "origin": entry.origin,
                    "state": entry.state,
                    "isolate": list(entry.isolate),
                    "active": entry.active,
                    "manifest": entry.manifest.to_dict(),
                }
                for entry in self.entries
            ],
            "bundles": [digest.to_dict() for digest in self.bundles],
            "signatures": [signature.to_dict() for signature in self.signatures],
            "sandbox": self.sandbox.to_dict(),
        }


class CapabilityRegistry:
    """Mutable registry the kernel fills while booting and mounting plugins."""

    def __init__(self) -> None:
        self._entries: list[CapabilityEntry] = []
        self._issues: list[CapabilityIssue] = []
        self._bundles: list[BundleDigest] = []
        self._signatures: list[BundleSignature] = []

    @property
    def entries(self) -> tuple[CapabilityEntry, ...]:
        """Registered entries in registration order."""
        return tuple(self._entries)

    @property
    def issues(self) -> tuple[CapabilityIssue, ...]:
        """Issues discovered while indexing the composition."""
        return tuple(self._issues)

    @property
    def bundles(self) -> tuple[BundleDigest, ...]:
        """Recorded bundle.toml digests in recording order."""
        return tuple(self._bundles)

    @property
    def signatures(self) -> tuple[BundleSignature, ...]:
        """Recorded bundle.toml signature states in recording order."""
        return tuple(self._signatures)

    def register(
        self,
        manifest: CapabilityManifest,
        *,
        plugin_id: str = "",
        origin: str = "",
        state: str = ENTRY_ENABLED,
        isolate: Iterable[str] = (),
        active: bool = False,
    ) -> CapabilityEntry:
        """Add one manifest to the registry."""
        entry = CapabilityEntry(
            plugin_id=plugin_id or manifest.plugin_id,
            manifest=manifest,
            origin=origin,
            state=state,
            isolate=tuple(isolate),
            active=active,
        )
        self._entries.append(entry)
        return entry

    def index_composition(self, composition: Composition) -> list[CapabilityIssue]:
        """Register every resolvable composition row (isolate path preserved).

        Rows that cannot be resolved are skipped: an enabled broken row still
        fails later through the normal mount path (with the original error),
        and a disabled/broken manifest row must not fail boot.  Enabled rows
        without a readable manifest produce an INVALID_MANIFEST diagnostic.
        """
        issues: list[CapabilityIssue] = []

        def walk(row: PluginRow, parent_isolate: tuple[str, ...], parent_disabled: bool) -> None:
            isolate = parent_isolate + tuple(row.isolate)
            disabled = parent_disabled or row.disabled is True
            if row.plugin:
                try:
                    manifest = build_manifest(resolve_row(row))
                except Exception as exc:  # noqa: BLE001 - reported as a diagnostic
                    if not disabled and not row.group:
                        issues.append(
                            CapabilityIssue(
                                kind=CapabilityIssueKind.INVALID_MANIFEST,
                                plugin_id=row.id,
                                message=(
                                    f"plugin {row.id!r} declares no readable capability manifest: "
                                    f"{type(exc).__name__}: {exc}"
                                ),
                                related=(row.plugin,),
                            )
                        )
                else:
                    state = ENTRY_DISABLED if disabled else (ENTRY_GROUP if row.group else ENTRY_ENABLED)
                    self.register(
                        manifest,
                        plugin_id=row.id,
                        origin=row.origin,
                        state=state,
                        isolate=isolate,
                        active=False,
                    )
            for child in row.children:
                walk(child, isolate, disabled)

        for row in composition.rows():
            walk(row, (), False)
        self._issues.extend(issues)
        return issues

    def mark_active(self, plugin_id: str, *, active: bool = True) -> None:
        """Mark the entries of `plugin_id` as live-mounted (or not)."""
        self._entries = [
            replace(entry, active=active)
            if entry.plugin_id == plugin_id and entry.active != active
            else entry
            for entry in self._entries
        ]

    def record_bundle_digests(self, digests: Iterable[BundleDigest]) -> list[CapabilityIssue]:
        """Record loaded bundle.toml digests and flag bad digest pins.

        A bundle without a declared digest is recorded as ``unverified`` and
        never blocks boot (backward compatible).  A valid digest that does not
        match the file bytes, or a malformed ``[bundle].digest`` value, adds a
        fatal issue so the caller's normal ``report().ok`` check refuses boot.
        """
        issues: list[CapabilityIssue] = []
        for digest in digests:
            self._bundles.append(digest)
            label = digest.bundle_id or digest.path
            if digest.state is BundleDigestState.MISMATCH:
                issue = CapabilityIssue(
                    kind=CapabilityIssueKind.BUNDLE_DIGEST_MISMATCH,
                    plugin_id=label,
                    message=(
                        f"bundle {label!r} at {digest.path} declares digest {digest.declared}, "
                        f"but its bundle.toml bytes hash to sha256:{digest.sha256}; refusing to "
                        "boot because the bundle changed after it was pinned"
                    ),
                    related=(digest.path,),
                )
            elif digest.state is BundleDigestState.INVALID:
                issue = CapabilityIssue(
                    kind=CapabilityIssueKind.INVALID_BUNDLE_DIGEST,
                    plugin_id=label,
                    message=(
                        f"bundle {label!r} at {digest.path} declares digest "
                        f"{digest.declared!r}; expected 'sha256:' followed by 64 lowercase hex "
                        "characters"
                    ),
                    related=(digest.path,),
                )
            else:
                continue
            self._issues.append(issue)
            issues.append(issue)
        return issues

    def record_bundle_signatures(self, signatures: Iterable[BundleSignature]) -> list[CapabilityIssue]:
        """Record loaded bundle.toml signature states and flag bad signatures.

        A bundle without a declared signature is recorded as ``unverified`` and
        never blocks boot (backward compatible).  A declared signature that does
        not match the file bytes, a malformed ``[bundle].signature`` value, or a
        declared signature with no configured key adds a fatal issue so the
        caller's normal ``report().ok`` check refuses boot.  This is an integrity
        and authenticity check against a shared secret, not an OS sandbox: it
        cannot stop a trusted local process from editing the bundle and the key
        together.
        """
        issues: list[CapabilityIssue] = []
        for signature in signatures:
            self._signatures.append(signature)
            label = signature.bundle_id or signature.path
            if signature.state is BundleSignatureState.MISMATCH:
                issue = CapabilityIssue(
                    kind=CapabilityIssueKind.BUNDLE_SIGNATURE_MISMATCH,
                    plugin_id=label,
                    message=(
                        f"bundle {label!r} at {signature.path} declares signature "
                        f"{signature.declared}, but its bundle.toml bytes do not match that "
                        "HMAC-SHA256 signature; refusing to boot because the bundle was "
                        "modified or signed with a different key"
                    ),
                    related=(signature.path,),
                )
            elif signature.state is BundleSignatureState.INVALID:
                issue = CapabilityIssue(
                    kind=CapabilityIssueKind.INVALID_BUNDLE_SIGNATURE,
                    plugin_id=label,
                    message=(
                        f"bundle {label!r} at {signature.path} declares signature "
                        f"{signature.declared!r}; expected 'hmac-sha256:' followed by 64 "
                        "lowercase hex characters"
                    ),
                    related=(signature.path,),
                )
            elif signature.state is BundleSignatureState.MISSING_KEY:
                issue = CapabilityIssue(
                    kind=CapabilityIssueKind.BUNDLE_SIGNATURE_KEY_MISSING,
                    plugin_id=label,
                    message=(
                        f"bundle {label!r} at {signature.path} declares signature "
                        f"{signature.declared} but no signing key is configured; set "
                        "DEEPPYRO_BUNDLE_SIGNING_KEY or DEEPPYRO_BUNDLE_SIGNING_KEY_FILE to "
                        "verify it, or remove [bundle].signature to keep the bundle unpinned"
                    ),
                    related=(signature.path,),
                )
            else:
                continue
            self._issues.append(issue)
            issues.append(issue)
        return issues

    def report(self, *, available_services: Iterable[str] = ()) -> CapabilityReport:
        """Validate the declared graph and snapshot it."""
        entries = tuple(self._entries)
        available = frozenset(str(name) for name in available_services)
        issues: list[CapabilityIssue] = list(self._issues)
        issues.extend(_provider_issues(entries, available))
        return CapabilityReport(
            entries=entries,
            issues=tuple(issues),
            available_services=available,
            bundles=tuple(self._bundles),
            signatures=tuple(self._signatures),
        )


def _provider_issues(
    entries: tuple[CapabilityEntry, ...],
    available: frozenset[str],
) -> list[CapabilityIssue]:
    issues: list[CapabilityIssue] = []
    enabled = [entry for entry in entries if entry.state == ENTRY_ENABLED]
    runtime = [entry for entry in entries if entry.state == ENTRY_RUNTIME]

    # Providers visible to dependents: enabled rows and host-mounted plugins.
    live = enabled + runtime
    declared: dict[str, list[CapabilityEntry]] = {}
    for entry in entries:
        for service in entry.manifest.provides:
            declared.setdefault(service, []).append(entry)
    providers: dict[str, list[CapabilityEntry]] = {}
    for entry in live:
        for service in entry.manifest.provides:
            providers.setdefault(service, []).append(entry)

    # Duplicate providers: same service within the same isolate scope.
    buckets: dict[tuple[str, tuple[str, ...]], list[CapabilityEntry]] = {}
    for entry in enabled:
        for service in entry.manifest.provides:
            buckets.setdefault((service, entry.isolate), []).append(entry)
    for (service, isolate), owners in buckets.items():
        if len(owners) < 2:
            continue
        ids = [owner.plugin_id for owner in owners]
        scope = f" in isolate scope {list(isolate)!r}" if isolate else ""
        issues.append(
            CapabilityIssue(
                kind=CapabilityIssueKind.DUPLICATE_PROVIDE,
                plugin_id=ids[0],
                message=(
                    f"service {service!r} is provided by more than one plugin{scope}: " + ", ".join(ids)
                ),
                related=tuple(ids[1:]),
            )
        )

    for entry in enabled:
        manifest = entry.manifest
        for service in manifest.requires:
            if service in manifest.provides or service in available:
                continue
            constraint = manifest.version_requirement(service)
            candidates = providers.get(service, [])
            if candidates:
                if not constraint:
                    continue
                matching = [
                    candidate
                    for candidate in candidates
                    if version_satisfies(candidate.manifest.version, constraint)
                ]
                if matching:
                    continue
                offered = ", ".join(
                    f"{candidate.plugin_id} {candidate.manifest.version}" for candidate in candidates
                )
                issues.append(
                    CapabilityIssue(
                        kind=CapabilityIssueKind.VERSION_MISMATCH,
                        plugin_id=entry.plugin_id,
                        message=(
                            f"plugin {entry.plugin_id!r} requires service {service!r} {constraint}, "
                            f"but the available providers do not satisfy it: {offered}"
                        ),
                        related=tuple(candidate.plugin_id for candidate in candidates),
                    )
                )
                continue
            pending = declared.get(service, [])
            if pending:
                ids = [candidate.plugin_id for candidate in pending]
                issues.append(
                    CapabilityIssue(
                        kind=CapabilityIssueKind.PENDING_PROVIDER,
                        plugin_id=entry.plugin_id,
                        message=(
                            f"plugin {entry.plugin_id!r} requires service {service!r}, whose provider(s) "
                            f"are not active: {', '.join(ids)}"
                        ),
                        related=tuple(ids),
                    )
                )
                continue
            issues.append(
                CapabilityIssue(
                    kind=CapabilityIssueKind.MISSING_REQUIRE,
                    plugin_id=entry.plugin_id,
                    message=(
                        f"plugin {entry.plugin_id!r} requires service {service!r}, but no plugin in this "
                        "composition provides it"
                    ),
                )
            )
    return issues


__all__ = [
    "BUNDLE_SIGNING_KEY_ENV",
    "BUNDLE_SIGNING_KEY_FILE_ENV",
    "DIGEST_PLACEHOLDER",
    "ENTRY_DISABLED",
    "ENTRY_ENABLED",
    "ENTRY_GROUP",
    "ENTRY_RUNTIME",
    "FATAL_ISSUES",
    "IMPLICIT_SERVICES",
    "SIGNATURE_PLACEHOLDER",
    "BundleDigest",
    "BundleDigestState",
    "BundleSignature",
    "BundleSignatureState",
    "BundleSigningKeyError",
    "CapabilityEntry",
    "CapabilityIssue",
    "CapabilityIssueKind",
    "CapabilityRegistry",
    "CapabilityReport",
    "SandboxBoundary",
    "bundle_digest_bytes",
    "bundle_signature_bytes",
    "bundle_signature_payload",
    "host_sandbox_boundary",
    "inspect_bundle_digest",
    "inspect_bundle_digests",
    "inspect_bundle_signature",
    "inspect_bundle_signatures",
    "resolve_bundle_signing_key",
]
